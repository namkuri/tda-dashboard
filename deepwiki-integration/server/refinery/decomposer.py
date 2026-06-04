"""[r223] 분해 (Decompose) — vault 청크를 LLM 으로 노드 추출.

전략 (스펙 §6):
1. vault 본문 합치고 청크 4KB 단위 분할
2. 각 청크별 LLM 호출 → 노드 추출 (메타 누출 가드 + vault cross-check)
3. 전역 dedup (제목 정규화 + 임베딩 유사도 0.85+)
4. 트리 합성 (parent_id 결정 — LLM)
5. SSE: stage / chunk_progress / node_added / dedup / done

노드 스키마:
{
  id, title, summary, source_refs[{vault_doc_id, span_text}],
  ai_confidence, auto_category_hint, kind, parent_id,
  created_by="auto:llm:{model}", history[]
}
"""
import json
import re
import hashlib
import time
from typing import AsyncIterator, Dict, Any, List, Optional

from ollama_client import get_ollama
from llm_router import get_llm  # [r226] Gemini/Ollama 라우팅
from ._author_guard import history_entry, llm_author


# 메타 누출 키워드 (r215 패턴 재사용 + 정련소 컨텍스트 확장)
_META_LEAK = [
    # [r228] 연구 본문에 흔한 단어(vault/청크/단락/노드 추출 등)는 제거 — 과필터로
    # 정상 노드까지 걸러지던 문제. 진짜 프롬프트 고유 어구만 유지.
    "mutually exclusive", "collectively exhaustive",
    "json only", "===followups===",
    "auto_category_hint", "supportedgenerationmethods",
]


def _is_meta_leak(text: str) -> bool:
    """[r228] title 전용 — summary 는 검사 안 함(일반 설명 정상).
    완전 일치 또는 명백한 프롬프트 어구만 차단. 부분 포함 과필터 방지.
    """
    if not text:
        return False
    t = text.strip().lower()
    # 완전 일치
    if t in ("mece", "node", "title", "summary"):
        return True
    return any(kw in t for kw in _META_LEAK)


def _normalize_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").lower().strip())


def _strip_html_css(text: str) -> str:
    """[r231] HTML 본문에서 style/script/CSS/태그 제거 — vault 가 .html 일 때
    <style> 의 CSS 코드가 본문으로 새어들어가 'CSS 스타일링/폰트 설정' 같은
    무관 노드가 추출되던 문제 차단."""
    if not text:
        return text
    t = text
    # style/script 블록 통째 제거 (CSS·JS 코드)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"<script[^>]*>.*?</script>", " ", t, flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.DOTALL)
    # 블록 경계 → 줄바꿈 (문단 보존)
    t = re.sub(r"</(p|div|li|h[1-6]|tr|section|article|figure|figcaption)>", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"<(br|hr)[^>]*>", "\n", t, flags=re.IGNORECASE)
    # 나머지 태그 제거
    t = re.sub(r"<[^>]+>", " ", t)
    # 엔티티
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
           .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    # 공백 정리
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _looks_html(text: str) -> bool:
    low = (text or "")[:2000].lower()
    return ("<style" in low) or ("<!doctype" in low) or ("<html" in low) or ("<body" in low)


def _chunk_vault(vault_docs: List[Dict[str, Any]], chunk_size: int = 6000) -> List[Dict[str, Any]]:
    """vault 본문 합쳐 청크 단위 분할. 청크마다 출처 doc 정보 보존.

    [r231] HTML 이면 style/script/태그 제거 후 청크. chunk_size 6000 으로 키워
    청크 수↓(노드 과다 완화). 한 줄이 chunk_size 초과면 강제 글자 단위 분할.
    """
    out = []
    for vd in vault_docs:
        body = (vd.get("content") or "").strip()
        if not body:
            continue
        # [r231] HTML 본문이면 CSS/script/태그 제거
        if _looks_html(body):
            body = _strip_html_css(body)
            if not body.strip():
                continue
        # 매우 긴 한 줄 강제 분할
        # 줄단위 → 누적 → chunk_size 초과 시 split
        lines = body.split("\n")
        normalized: List[str] = []
        for ln in lines:
            if len(ln) > chunk_size:
                # 글자 단위 strict split
                for i in range(0, len(ln), chunk_size):
                    normalized.append(ln[i:i + chunk_size])
            else:
                normalized.append(ln)
        buf: List[str] = []
        buf_len = 0
        for line in normalized:
            ln = len(line) + 1
            if buf_len + ln > chunk_size and buf:
                out.append({"vault_doc_id": vd.get("id"), "vault_title": vd.get("title"), "text": "\n".join(buf)})
                buf = [line]
                buf_len = ln
            else:
                buf.append(line)
                buf_len += ln
        if buf:
            out.append({"vault_doc_id": vd.get("id"), "vault_title": vd.get("title"), "text": "\n".join(buf)})
    return out


_SYSTEM_DECOMPOSE = """당신은 연구 문서에서 **핵심 개념(대분류·중분류)만** 골라내는 도구입니다.

[가장 중요한 규칙 — 핵심만]
- 이 청크의 **대주제·중주제·핵심 결정·핵심 가설·핵심 리스크만** 추출합니다.
- ❌ 사소한 디테일, 예시, 부연 설명, 형식·UI·스타일·CSS·폰트·레이아웃, 단순 나열 항목,
  반복되는 개념은 노드로 만들지 마세요.
- 청크 하나당 **2~5개**가 적정. 정말 핵심이 없으면 0개. 절대 10개 넘기지 마세요.
- "이게 문서 전체에서 중요한가?"를 스스로 물어 통과한 것만 노드화.

[엄격 규칙]
1. **vault 청크 안 텍스트만 노드화**. 청크에 없는 일반 지식·메타 어구·시스템 어구 금지.
2. 각 노드 title 은 1~5 단어 키워드. 서술 금지.
3. 출력은 ```json ... ``` 블록 안 JSON 한 개:
```json
{
  "nodes": [
    {
      "title": "핵심 키워드",
      "summary": "1~2 문장 요약",
      "span_text": "청크에서 가장 관련 깊은 30~80자 원문 발췌",
      "ai_confidence": 0~100,
      "auto_category_hint": "canon|hyp|later|cut",
      "kind": "concept|hypothesis|risk|decision|fact|question"
    }
  ]
}
```
4. canon = 확정된 핵심 사실/시스템. hyp = 핵심 가설/playtest 필요. later = MVP 이후. cut = 폐기/범위 밖.
5. risk = 핵심 리스크, decision = 핵심 결정, fact = 핵심 사실, question = 핵심 미해결 질문, concept = 핵심 개념.
"""


_SYSTEM_COMPOSE_TREE = """당신은 노드들을 계층 트리로 합성하는 도구입니다.

[규칙]
1. 입력 노드들의 parent_id 를 결정. 비슷한 주제는 같은 parent 아래로.
2. 새 카테고리 노드 추가 가능 (e.g. "1.아키텍처", "2.메커니즘") — 단, 1~5 단어.
3. 깊이 최대 4. 1레벨 카테고리 4~8 개.
4. 출력 JSON:
```json
{
  "categories": [
    { "title": "카테고리 키워드", "node_ids": ["n_xxx", "n_yyy"] }
  ]
}
```
"""


async def decompose(
    *,
    session_id: str,
    project_id: Optional[str],
    vault_docs: List[Dict[str, Any]],  # [{id, title, content}]
    model: Optional[str] = None,
    user_id: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """vault → 노드 트리 추출. SSE 이벤트."""
    ollama = get_llm(model)  # [r226] gemini-* 면 Gemini, 아니면 Ollama
    author = llm_author(model or "default")
    started = time.time()

    chunks = _chunk_vault(vault_docs, chunk_size=4000)
    total_chunks = len(chunks)
    if not total_chunks:
        yield {"event": "error", "message": "vault 본문이 비어있습니다."}
        return

    yield {
        "event": "stage", "stage": "decompose",
        "message": f"vault {len(vault_docs)}개, 청크 {total_chunks}개 분해 시작",
        "total_chunks": total_chunks,
    }

    all_nodes: List[Dict[str, Any]] = []
    skipped_meta = 0
    _parse_fail = 0  # [r228] JSON 파싱 실패 청크 수

    for idx, ch in enumerate(chunks):
        yield {
            "event": "chunk_start", "current": idx + 1, "total": total_chunks,
            "vault_title": ch["vault_title"],
        }
        user_prompt = f"""다음 vault 청크에서 노드를 추출하세요.

<<<VAULT_CHUNK {idx + 1}/{total_chunks}>>>
출처: {ch['vault_title']}
{ch['text']}
<<<END>>>

JSON 한 개만 출력. **핵심 개념만 2~5개** (사소·스타일·형식·예시 제외, 없으면 0개).
"""
        try:
            buf = ""
            async for delta in ollama.chat_stream(
                messages=[
                    {"role": "system", "content": _SYSTEM_DECOMPOSE},
                    {"role": "user", "content": user_prompt},
                ],
                model=model,
                temperature=0.3,
            ):
                buf += delta
        except Exception as e:
            yield {"event": "warn", "message": f"청크 {idx + 1} LLM 실패: {e}"}
            continue

        parsed = _extract_json(buf)
        if not parsed or not isinstance(parsed.get("nodes"), list):
            # [r228] 파싱 실패 진단 — raw 앞부분 샘플로 원인 추적 (LLM이 JSON 안 만듦 등)
            _parse_fail += 1
            raw_head = (buf or "").strip()[:160].replace("\n", " ")
            yield {"event": "warn", "message": f"청크 {idx + 1} JSON 파싱 실패 · raw: {raw_head or '(빈 응답)'}"}
            continue

        chunk_node_n = 0
        for n in parsed["nodes"]:
            if not isinstance(n, dict):
                continue
            title = (n.get("title") or "").strip()
            if not title:
                continue
            # [r228] 메타 누출 가드 — title 만 검사 (summary 는 일반 설명이라 제외)
            if _is_meta_leak(title):
                skipped_meta += 1
                continue
            # vault cross-check — span_text 가 실제 청크에 있는지
            span = (n.get("span_text") or "").strip()
            if span and span[:20].lower() not in ch["text"].lower():
                # 의심스럽지만 일단 보존, 신뢰도 절감
                if isinstance(n.get("ai_confidence"), (int, float)):
                    n["ai_confidence"] = max(0, int(n["ai_confidence"]) - 20)
            node = {
                "id": _uid("n"),
                "title": title[:60],
                "summary": (n.get("summary") or "")[:300],
                "source_refs": [{
                    "vault_doc_id": ch["vault_doc_id"],
                    "vault_title": ch["vault_title"],
                    "span_text": span[:300],
                }],
                "ai_confidence": int(n.get("ai_confidence") or 50),
                "auto_category_hint": (n.get("auto_category_hint") or "later").lower(),
                "kind": (n.get("kind") or "concept").lower(),
                "parent_id": None,
                "created_by": author,
                "created_at": _now_iso(),
                "history": [history_entry("extracted", author, f"chunk {idx + 1}/{total_chunks}")],
            }
            all_nodes.append(node)
            chunk_node_n += 1
            yield {"event": "node_added", "node": node}
        yield {
            "event": "chunk_done", "current": idx + 1, "total": total_chunks,
            "nodes_so_far": len(all_nodes), "chunk_nodes": chunk_node_n,
            "percent": round((idx + 1) / total_chunks * 100, 1),
        }

    # Dedup — 제목 정규화 기반
    yield {"event": "stage", "stage": "dedup", "message": f"노드 {len(all_nodes)}개 중복 검사…"}
    seen_title: Dict[str, Dict[str, Any]] = {}
    merged = 0
    for n in all_nodes:
        key = _normalize_title(n["title"])
        if key in seen_title:
            # 기존 노드에 source_refs 추가
            seen_title[key]["source_refs"].extend(n["source_refs"])
            # 신뢰도는 높은 쪽 유지
            if n["ai_confidence"] > seen_title[key]["ai_confidence"]:
                seen_title[key]["ai_confidence"] = n["ai_confidence"]
            merged += 1
        else:
            seen_title[key] = n
    deduped_nodes = list(seen_title.values())
    yield {"event": "dedup_done", "merged": merged, "kept": len(deduped_nodes)}

    # 트리 합성 — [r229] 노드 많으면 LLM 미사용 로직 그룹핑(즉시), 적으면 LLM(스트리밍+타임아웃)
    categories: List[Dict[str, Any]] = []

    def _mk_cat(title: str) -> Dict[str, Any]:
        return {
            "id": _uid("cat"), "title": title[:40], "summary": "", "source_refs": [],
            "ai_confidence": 100, "auto_category_hint": "canon", "kind": "category",
            "parent_id": None, "created_by": author, "created_at": _now_iso(),
            "history": [history_entry("category_created", author)],
        }

    LLM_TREE_MAX_NODES = 120   # 이보다 많으면 로직 그룹핑
    if len(deduped_nodes) > LLM_TREE_MAX_NODES:
        # [r229] 로직 그룹핑 — LLM 미사용. auto_category_hint + kind 로 즉시 분류.
        yield {"event": "stage", "stage": "compose_tree",
               "message": f"카테고리 트리 합성 — 로직 그룹핑 (노드 {len(deduped_nodes)}개 > {LLM_TREE_MAX_NODES}, LLM 미사용·즉시)"}
        # hint+kind 별 버킷
        bucket_label = {
            ("canon", "risk"): "리스크 & 방어",
            ("hyp", "risk"): "리스크 & 방어",
            ("canon", "concept"): "핵심 개념",
            ("canon", "fact"): "사실 & 근거",
            ("canon", "decision"): "결정 사항",
            ("hyp", "hypothesis"): "가설 (검증 대기)",
            ("hyp", "concept"): "가설 (검증 대기)",
            ("later", "concept"): "백로그",
            ("cut", "concept"): "폐기 후보",
            ("canon", "question"): "미해결 질문",
        }
        cat_by_label: Dict[str, Dict[str, Any]] = {}
        for n in deduped_nodes:
            hint = (n.get("auto_category_hint") or "later")
            kind = (n.get("kind") or "concept")
            label = bucket_label.get((hint, kind))
            if not label:
                label = {"canon": "확정 항목", "hyp": "가설 (검증 대기)",
                         "later": "백로그", "cut": "폐기 후보"}.get(hint, "기타")
            if label not in cat_by_label:
                cat_by_label[label] = _mk_cat(label)
            n["parent_id"] = cat_by_label[label]["id"]
        categories = list(cat_by_label.values())
        yield {"event": "tree_composed", "method": "logic", "categories": len(categories)}
    else:
        # [r229] LLM 트리 합성 — 스트리밍 진행 + 90초 타임아웃. 실패해도 노드 보존.
        yield {"event": "stage", "stage": "compose_tree",
               "message": f"카테고리 트리 합성 — LLM 호출 (노드 {len(deduped_nodes)}개)"}
        node_listing = "\n".join(
            f"- {n['id']}: {n['title']} ({n['kind']}, {n['auto_category_hint']})"
            for n in deduped_nodes
        )
        user_prompt = f"""다음 노드 {len(deduped_nodes)}개를 카테고리 트리로 합성:

{node_listing}

JSON 한 개. categories 배열, 각 category 는 1~5 단어 title + 노드 ID 배열."""
        buf = ""
        try:
            import asyncio as _asyncio

            async def _consume():
                nonlocal buf
                async for delta in ollama.chat_stream(
                    messages=[
                        {"role": "system", "content": _SYSTEM_COMPOSE_TREE},
                        {"role": "user", "content": user_prompt},
                    ],
                    model=model, temperature=0.3,
                ):
                    buf += delta

            task = _asyncio.ensure_future(_consume())
            # 0.8초마다 진행(받은 글자수) 보고. 90초 타임아웃.
            waited = 0.0
            while not task.done():
                await _asyncio.sleep(0.8)
                waited += 0.8
                yield {"event": "tree_progress", "received_chars": len(buf), "waited_sec": round(waited, 1)}
                if waited > 90:
                    task.cancel()
                    yield {"event": "warn", "message": "트리 합성 90초 초과 — 로직 그룹핑으로 폴백"}
                    break
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc:
                    yield {"event": "warn", "message": f"트리 합성 LLM 실패: {exc}"}
            parsed = _extract_json(buf) if buf else None
            if parsed and isinstance(parsed.get("categories"), list):
                for cat in parsed["categories"]:
                    if not isinstance(cat, dict): continue
                    title = (cat.get("title") or "").strip()
                    node_ids = cat.get("node_ids") or []
                    if title and isinstance(node_ids, list) and not _is_meta_leak(title):
                        cat_node = _mk_cat(title)
                        categories.append(cat_node)
                        for nid in node_ids:
                            for n in deduped_nodes:
                                if n["id"] == nid:
                                    n["parent_id"] = cat_node["id"]
                                    break
            # LLM 결과 비었으면 로직 폴백
            if not categories:
                yield {"event": "warn", "message": "LLM 카테고리 비어있음 — hint 기반 로직 그룹핑 폴백"}
                cat_by_hint: Dict[str, Dict[str, Any]] = {}
                for n in deduped_nodes:
                    label = {"canon": "확정 항목", "hyp": "가설 (검증 대기)",
                             "later": "백로그", "cut": "폐기 후보"}.get(n.get("auto_category_hint") or "later", "기타")
                    if label not in cat_by_hint:
                        cat_by_hint[label] = _mk_cat(label)
                    n["parent_id"] = cat_by_hint[label]["id"]
                categories = list(cat_by_hint.values())
            yield {"event": "tree_composed", "method": "llm" if categories else "none", "categories": len(categories)}
        except Exception as e:
            yield {"event": "warn", "message": f"트리 합성 실패(노드는 보존): {e}"}

    all_final = categories + deduped_nodes
    elapsed = time.time() - started
    # [r228] 노드 0개 진단 — 원인 힌트
    diag_hint = None
    if not deduped_nodes:
        if _parse_fail >= total_chunks:
            diag_hint = (
                f"⚠ 모든 청크({total_chunks}개)에서 LLM 이 유효한 JSON 을 만들지 못했습니다. "
                "모델이 분해에 부적합할 수 있습니다 — 🌟 Gemini Flash 또는 더 큰 모델로 재시도하세요. "
                "(qwen2.5-coder 는 코드용이라 한국어 연구 문서 JSON 추출이 약할 수 있음)"
            )
        elif skipped_meta > 0:
            diag_hint = f"⚠ 추출된 노드가 모두 메타 누출로 걸러졌습니다(skip {skipped_meta}). 모델 변경 권장."
        else:
            diag_hint = "⚠ 노드 0개 — vault 본문이 너무 짧거나 모델이 빈 응답. 본문 확인 또는 모델 변경."
    yield {
        "event": "done",
        "nodes": all_final,
        "node_count": len(all_final),
        "category_count": len(categories),
        "leaf_count": len(deduped_nodes),
        "skipped_meta": skipped_meta,
        "parse_fail_chunks": _parse_fail,
        "total_chunks": total_chunks,
        "diag_hint": diag_hint,
        "elapsed_sec": round(elapsed, 1),
        "vault_doc_ids": [vd.get("id") for vd in vault_docs],
    }


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text: return None
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    cand = m.group(1) if m else None
    if not cand:
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            cand = text[first:last + 1]
    if not cand: return None
    try:
        return json.loads(cand)
    except Exception:
        try:
            fixed = re.sub(r",\s*([}\]])", r"\1", cand)
            return json.loads(fixed)
        except Exception:
            return None


_uid_counter = [0]
def _uid(prefix: str) -> str:
    _uid_counter[0] += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_uid_counter[0]}"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
