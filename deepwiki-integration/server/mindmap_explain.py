"""[r217] 마인드맵 노드 클릭 → NotebookLM 스타일 AI 설명.

흐름:
  1) 노드 컨텍스트 빌드 — central + 부모 체인 + 자기 + 형제 + 자식
  2) 소스 문서 본문 fetch — doc.meta.mindmap_sources 의 doc_id → wiki_docs
  3) LLM 호출 — 본문 발췌 [1][2]... + 노드 위치 → 설명 마크다운
     * 인용 시 [^N] 각주
     * 마지막에 ===FOLLOWUPS=== { "followups": ["...", "...", "..."] }
  4) SSE 이벤트: stage/sources/delta/followups/done

원칙:
  - 본문 발췌만 근거 (할루시네이션 금지). 본문에 없는 사실은 "본문에서는 명시되지
    않았습니다" 명시.
  - 출력은 한국어 (사용자 환경 기본).
"""
import json
import re
from typing import AsyncIterator, Dict, Any, List, Optional

from ollama_client import get_ollama
from supabase_store import get_store


_SYSTEM_PROMPT = """당신은 NotebookLM 스타일의 학습 도우미입니다. 사용자가 마인드맵에서 특정 노드를
클릭했을 때, 그 노드의 의미를 **소스 문서 본문만 근거로** 설명합니다.

[엄격 규칙]
1. 소스 문서에 명시된 내용만 사용. 본문 밖 일반 지식 사용 금지.
2. 인용은 `[^1]`, `[^2]` 같은 각주 형식. 숫자는 [Source documents] 의 번호와 일치.
3. 본문에 명시되지 않은 정보가 필요하면 "본문에서는 ~ 명시되지 않습니다" 라고 답하세요.
4. 한국어로 응답. 마크다운 사용 (불릿/굵게/기울임).
5. 응답 마지막에 반드시 후속 질문 3개를 다음 형식으로 정확히 출력:

```
===FOLLOWUPS===
{"followups": ["질문1", "질문2", "질문3"]}
```

후속 질문은 클릭한 노드를 더 깊이 파고들거나 인접 개념과 비교하는 자연스러운 다음
단계여야 합니다. 한국어 한 문장씩, 의문문으로.

[응답 형식]
- 첫 줄: 노드 의미 한 줄 요약 (굵게)
- 다음 단락: 본문 인용 + 각주 (3~6문장)
- 필요시 불릿
- 마지막: ===FOLLOWUPS=== 블록
"""


def _flatten_node_paths(branches: List[Dict[str, Any]], parent_path=None, parent_titles=None) -> List[Dict[str, Any]]:
    """branches 트리 → 평탄화된 [{title, path_titles, depth, children_titles, sibling_titles}]."""
    out = []
    parent_path = parent_path or []
    parent_titles = parent_titles or []
    for i, br in enumerate(branches or []):
        if not isinstance(br, dict):
            continue
        title = (br.get("title") or "").strip()
        if not title:
            continue
        path = parent_path + [str(i)]
        titles_chain = parent_titles + [title]
        children = br.get("children") or []
        children_titles = [c.get("title") for c in children if isinstance(c, dict)]
        siblings = [b.get("title") for b in branches if isinstance(b, dict) and b is not br]
        out.append({
            "title": title,
            "path_titles": titles_chain,
            "depth": len(titles_chain),
            "children_titles": children_titles,
            "sibling_titles": siblings,
            "origin": br.get("origin") or [],
            "summary": br.get("summary") or "",
        })
        if children:
            out.extend(_flatten_node_paths(children, path, titles_chain))
    return out


def _find_node(branches: List[Dict[str, Any]], node_title: str, node_path: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    flat = _flatten_node_paths(branches)
    # 1) node_path (titles chain) 우선
    if node_path:
        for n in flat:
            if n["path_titles"] == node_path:
                return n
    # 2) title 만으로 첫 매칭
    for n in flat:
        if n["title"] == node_title:
            return n
    # 3) lowercased 폴백
    nt = (node_title or "").strip().lower()
    for n in flat:
        if n["title"].strip().lower() == nt:
            return n
    return None


async def _fetch_source_docs(project_id: Optional[str], doc_ids: List[str]) -> List[Dict[str, Any]]:
    """wiki_docs 에서 소스 문서 본문 fetch."""
    store = get_store()
    ids = [d for d in (doc_ids or []) if isinstance(d, str) and d]
    if not ids:
        return []
    try:
        q = store.client.table("wiki_docs").select("id,title,content").in_("id", ids)
        if project_id:
            q = q.eq("project_id", project_id)
        res = q.execute()
        rows = res.data or []
    except Exception:
        return []
    # id 순서 보존
    by_id = {r["id"]: r for r in rows}
    out = []
    for did in ids:
        r = by_id.get(did)
        if not r:
            continue
        out.append({
            "id": r.get("id"),
            "title": r.get("title") or "(제목 없음)",
            "content": (r.get("content") or "")[:12000],
        })
    return out


def _select_relevant_excerpt(content: str, node_title: str, sibling_titles: List[str], max_chars: int = 1800) -> str:
    """소스 본문에서 노드 제목·형제 제목 근처를 발췌. 단순 키워드 윈도우."""
    if not content:
        return ""
    text = content
    # 단순 휴리스틱 — 노드 title 포함 부근 ±600자 윈도우 2~3개 추출, 부족하면 형제도 시도
    candidates = []
    needles = [node_title] + [s for s in (sibling_titles or []) if s]
    seen_pos = set()
    for needle in needles:
        if not needle:
            continue
        idx = 0
        while True:
            j = text.lower().find(needle.lower(), idx)
            if j < 0:
                break
            start = max(0, j - 400)
            end = min(len(text), j + 800)
            key = (start // 400, end // 400)
            if key not in seen_pos:
                seen_pos.add(key)
                candidates.append(text[start:end])
            idx = j + 1
            if len(candidates) >= 3:
                break
        if len(candidates) >= 3:
            break
    if candidates:
        joined = "\n…\n".join(candidates)
        return joined[:max_chars]
    # 매칭 없으면 앞부분 1800자
    return text[:max_chars]


def _build_user_prompt(node: Dict[str, Any], sources: List[Dict[str, Any]], central_title: str) -> str:
    path = " > ".join(node.get("path_titles") or [node.get("title") or "?"])
    children = ", ".join((node.get("children_titles") or [])[:8]) or "(없음)"
    siblings = ", ".join((node.get("sibling_titles") or [])[:6]) or "(없음)"
    src_blocks = []
    for i, s in enumerate(sources, 1):
        excerpt = _select_relevant_excerpt(s["content"], node.get("title") or "", node.get("sibling_titles") or [])
        src_blocks.append(f"[{i}] **{s['title']}**\n\n{excerpt}")
    sources_md = "\n\n---\n\n".join(src_blocks) if src_blocks else "(등록된 소스 없음)"
    return f"""[중심 주제]
{central_title}

[클릭한 노드]
경로: {path}
형제 노드: {siblings}
직속 자식: {children}
{("문서 출처 표시: " + ", ".join(node.get("origin") or [])) if node.get("origin") else ""}

[Source documents]
{sources_md}

[Task]
위 [클릭한 노드] 의 의미를 본문 발췌 기반으로 한국어 마크다운으로 설명하세요.
- 각주 형식: [^1], [^2] 등
- 본문에 없는 내용은 "본문에 명시되지 않음" 표시
- 마지막에 ===FOLLOWUPS=== JSON 블록으로 후속 질문 3개
"""


async def explain_node(
    *,
    project_id: Optional[str],
    diagram_branches: List[Dict[str, Any]],
    central: str,
    node_title: str,
    node_path: Optional[List[str]],
    source_doc_ids: List[str],
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    ollama = get_ollama()

    # 1) 노드 컨텍스트
    yield {"event": "stage", "stage": "locate", "message": f"노드 '{node_title}' 위치 확인 중…"}
    node = _find_node(diagram_branches or [], node_title, node_path)
    if not node:
        node = {"title": node_title, "path_titles": [central, node_title], "children_titles": [], "sibling_titles": [], "origin": [], "summary": ""}

    # 2) 소스 본문
    yield {"event": "stage", "stage": "fetch_sources", "message": f"소스 문서 {len(source_doc_ids)}개 본문 수집…"}
    sources = await _fetch_source_docs(project_id, source_doc_ids)
    # 프론트가 각주 점프에 쓰도록 정형 메타 yield
    src_meta = [
        {"idx": i + 1, "id": s["id"], "title": s["title"], "content_len": len(s["content"])}
        for i, s in enumerate(sources)
    ]
    yield {"event": "sources", "sources": src_meta}
    if not sources:
        yield {
            "event": "delta",
            "text": (
                "⚠ 이 마인드맵에 등록된 소스 문서가 없습니다.\n\n"
                "마인드맵 헤더의 **📚 소스 관리** 에서 소스를 추가한 뒤 다시 시도하세요.\n\n"
                "```\n===FOLLOWUPS===\n"
                '{"followups": ["이 마인드맵의 소스를 어떻게 추가하나요?", "마인드맵을 새로 만들려면?", "다른 문서에서 비슷한 개념은?"]}\n'
                "```\n"
            ),
        }
        yield {"event": "done"}
        return

    # 3) LLM 호출
    yield {"event": "stage", "stage": "llm", "message": "LLM 설명 생성 중…"}
    user_prompt = _build_user_prompt(node, sources, central)

    buf = ""
    in_followups = False
    followups_raw = ""
    try:
        async for delta in ollama.chat_stream(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            temperature=0.3,
        ):
            buf += delta
            # FOLLOWUPS 토큰 감지 — 본문 vs followups 분리 스트리밍
            if not in_followups:
                marker = "===FOLLOWUPS==="
                if marker in buf:
                    head, _, tail = buf.partition(marker)
                    # head 중 아직 emit 안 된 부분 (전체 - 이미 emit 한 분) 계산
                    # 단순화: head 전체를 한 번 더 보내지 않으려 chunk-by-chunk 누적은 생략 후
                    # 처음 발견 즉시 partition 만 함. 이 시점 이전 delta 는 이미 emit 됐다 가정.
                    in_followups = True
                    followups_raw = tail
                    # 토큰 자체와 뒷부분이 본문에 섞여 나가는 걸 피하려고 buf 를 head 로 리셋
                    continue
                # 안전: 마커 일부가 split 될 가능성 — 마지막 16자 보존
                safe_emit_until = max(0, len(buf) - 16)
                if safe_emit_until > 0:
                    chunk = buf[:safe_emit_until]
                    buf = buf[safe_emit_until:]
                    yield {"event": "delta", "text": chunk}
            else:
                followups_raw += delta
    except Exception as e:
        yield {"event": "warn", "message": f"LLM 스트림 중단: {e}"}
    # 4) 남은 본문 emit (FOLLOWUPS 못 만나고 끝난 경우)
    if not in_followups and buf:
        yield {"event": "delta", "text": buf}

    # 5) followups 파싱
    followups: List[str] = []
    if followups_raw:
        # ```json ... ``` 또는 raw JSON
        m = re.search(r"\{[\s\S]*?\}", followups_raw)
        if m:
            try:
                obj = json.loads(m.group(0))
                fu = obj.get("followups")
                if isinstance(fu, list):
                    followups = [str(q).strip() for q in fu if str(q).strip()][:3]
            except Exception:
                pass
    if not followups:
        # 폴백 — 기본 후속 질문
        followups = [
            f"'{node.get('title')}' 의 핵심 근거 한 문장만 골라 인용해줘",
            f"이 개념이 다른 노드와 어떻게 연결되는지 설명해줘",
            f"본문에서 명확히 확인되지 않는 점은 무엇이야?",
        ]
    yield {"event": "followups", "followups": followups}
    yield {"event": "done"}
