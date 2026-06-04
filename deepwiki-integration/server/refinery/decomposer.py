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


def _chunk_vault(vault_docs: List[Dict[str, Any]], chunk_size: int = 4000) -> List[Dict[str, Any]]:
    """vault 본문 합쳐 청크 단위 분할. 청크마다 출처 doc 정보 보존.

    1) 줄 단위로 누적 (chunk_size 근처에서 새 청크)
    2) 한 줄이 chunk_size 초과면 강제 글자 단위 분할
    """
    out = []
    for vd in vault_docs:
        body = (vd.get("content") or "").strip()
        if not body:
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


_SYSTEM_DECOMPOSE = """당신은 연구 문서를 정련해 마인드맵 노드로 추출하는 도구입니다.

[엄격 규칙]
1. **vault 청크 안 텍스트만 노드화**. 청크에 없는 일반 지식·메타 어구·시스템 메시지 어구는 노드가 될 수 없음.
2. 각 노드 title 은 1~5 단어 키워드. 서술 금지.
3. 출력은 ```json ... ``` 블록 안 JSON 한 개:
```json
{
  "nodes": [
    {
      "title": "키워드",
      "summary": "1~2 문장 요약",
      "span_text": "청크에서 가장 관련 깊은 30~80자 원문 발췌",
      "ai_confidence": 0~100,
      "auto_category_hint": "canon|hyp|later|cut",
      "kind": "concept|hypothesis|risk|decision|fact|question"
    }
  ]
}
```
4. canon = 확정된 사실/시스템. hyp = 가설/playtest 필요. later = 좋은 아이디어지만 MVP 이후. cut = 폐기/범위 밖.
5. risk = 경고/리스크, decision = 결정 사안, fact = 단순 사실, hypothesis = 검증 필요 가설, question = 미해결 질문, concept = 일반 개념.
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

JSON 한 개만 출력. nodes 배열에 최소 1개, 최대 12개 노드.
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

    # 트리 합성
    yield {"event": "stage", "stage": "compose_tree", "message": "카테고리 트리 합성 중…"}
    categories: List[Dict[str, Any]] = []
    try:
        node_listing = "\n".join(
            f"- {n['id']}: {n['title']} ({n['kind']}, {n['auto_category_hint']})"
            for n in deduped_nodes[:200]
        )
        user_prompt = f"""다음 노드 {len(deduped_nodes)}개를 카테고리 트리로 합성:

{node_listing}

JSON 한 개. categories 배열, 각 category 는 1~5 단어 title + 노드 ID 배열."""
        buf = ""
        async for delta in ollama.chat_stream(
            messages=[
                {"role": "system", "content": _SYSTEM_COMPOSE_TREE},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            temperature=0.3,
        ):
            buf += delta
        parsed = _extract_json(buf)
        if parsed and isinstance(parsed.get("categories"), list):
            for cat in parsed["categories"]:
                if not isinstance(cat, dict): continue
                title = (cat.get("title") or "").strip()
                node_ids = cat.get("node_ids") or []
                if title and isinstance(node_ids, list):
                    if _is_meta_leak(title): continue
                    cat_node = {
                        "id": _uid("cat"),
                        "title": title[:40],
                        "summary": "",
                        "source_refs": [],
                        "ai_confidence": 100,
                        "auto_category_hint": "canon",
                        "kind": "category",
                        "parent_id": None,
                        "created_by": author,
                        "created_at": _now_iso(),
                        "history": [history_entry("category_created", author)],
                    }
                    categories.append(cat_node)
                    # 자식 노드들의 parent_id 설정
                    for nid in node_ids:
                        for n in deduped_nodes:
                            if n["id"] == nid:
                                n["parent_id"] = cat_node["id"]
                                break
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
