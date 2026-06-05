"""[r256] ⑤ 위키 구조 도출 — 표준 분류(WIKI_TAX)로 게임요소를 Well-Defined 구조화.

위키는 작업·순서가 아니라 '분류체계에 따라 잘 정의된 게임요소'(의도 #8). 표준 분류는
프론트 WIKI_TAX(계층)로 주고, LLM 은 분해 키워드를 그 분류에 매핑해 **필요한 가지만**
문서 구조(목차)로 만든다. 본문은 ⑥에서. 각 문서는 출처 키워드(origin_node_ids)를
달아 Stream 계보(키워드↔위키)를 잇는다.
"""
import time
from typing import AsyncIterator, Dict, Any, List, Optional

from llm_router import get_llm
from .intake import _extract_json


_SYSTEM = """당신은 게임 기획 문서를 '프로젝트 위키'로 체계화하는 도구입니다.
분해된 키워드들을 표준 분류(WIKI_TAX)에 매핑하여, 게임요소가 잘 정의(Well-Defined)
되도록 위키 문서 '구조(목차)'만 설계합니다. 본문은 쓰지 않습니다. 한국어, JSON only.

[규칙]
1. 표준 분류(WIKI_TAX)의 id 중 적합한 것에 매핑(wiki_tax). 해당 없으면 가장 가까운 상위.
2. 내용이 있는 분류만 문서로(필요한 가지만). 빈 분류 강제 생성 금지.
3. 각 문서: title, wiki_tax(분류 id), summary(1문장), outline(목차 3~8개:
   정의/규칙/파라미터/관계 등 well-defined 관점), origin_node_ids(출처 키워드 노드 id).
4. 작업·구현순서·일정은 포함하지 말 것(그건 작업 축).
5. 출력 JSON only.

```json
{ "docs":[{"title":"콤보 시스템","wiki_tax":"1.1","summary":"...",
   "outline":["정의","입력 규칙","판정 파라미터","캔슬 관계"],"origin_node_ids":["n_x"]}] }
```
"""


def _tax_block(wiki_tax: List[Dict[str, Any]], limit: int = 60) -> str:
    rows = []
    for t in (wiki_tax or [])[:limit]:
        s = t.get("s") or ""
        l = t.get("l") or ""
        rows.append(f"{t.get('i')}: {s}" + (f" ({l})" if l and l != s else ""))
    return "\n".join(rows) if rows else "(표준 분류 미제공)"


def _nodes_block(nodes: List[Dict[str, Any]], limit: int = 60) -> str:
    rows = []
    for n in (nodes or [])[:limit]:
        summ = (n.get("summary") or "").strip().replace("\n", " ")
        rows.append(f"[{n.get('id')}] {n.get('title','')}" + (f" — {summ[:80]}" if summ else ""))
    return "\n".join(rows)


def normalize_docs(parsed: Any, valid_node_ids: set, valid_tax: set) -> List[Dict[str, Any]]:
    if not isinstance(parsed, dict):
        return []
    out: List[Dict[str, Any]] = []
    for d in (parsed.get("docs") or []):
        if not isinstance(d, dict):
            continue
        tax = d.get("wiki_tax") or ""
        if valid_tax and tax not in valid_tax:
            # 가장 가까운 상위(접두) 매칭 시도
            tax = next((x for x in valid_tax if tax.startswith(str(x))), tax if not valid_tax else "")
        origins = [x for x in (d.get("origin_node_ids") or []) if (not valid_node_ids or x in valid_node_ids)]
        outline = [str(x)[:120] for x in (d.get("outline") or []) if str(x).strip()][:12]
        out.append({
            "id": _uid("wiki"),
            "title": (d.get("title") or "문서")[:120],
            "wiki_tax": tax,
            "summary": (d.get("summary") or "")[:300],
            "outline": outline,
            "origin_node_ids": origins,
        })
    return out


async def derive_wiki_structure(
    *,
    nodes: List[Dict[str, Any]],
    wiki_tax: Optional[List[Dict[str, Any]]] = None,
    context: str = "",
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """분해 노드 → WIKI_TAX 매핑 위키 구조(목차). SSE: stage / doc_proposed / done."""
    llm = get_llm(model)
    valid_node_ids = {n.get("id") for n in (nodes or [])}
    valid_tax = {str(t.get("i")) for t in (wiki_tax or [])}
    yield {"event": "stage", "message": f"키워드 {len(nodes or [])}개 → 위키 표준 분류 매핑"}

    prompt = (
        f"[표준 분류(WIKI_TAX) — wiki_tax 는 이 id 중에서]\n{_tax_block(wiki_tax or [])}\n\n"
        f"[기존 프로젝트]\n{context or '(없음)'}\n\n"
        f"[분해 키워드]\n{_nodes_block(nodes or [])}\n\n"
        "필요한 분류만 위키 문서 구조(목차) JSON 으로 출력하세요."
    )
    buf = ""
    try:
        async for delta in llm.chat_stream(
            messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
            model=model, temperature=0.3,
        ):
            buf += delta
            if len(buf) % 500 < len(delta):
                yield {"event": "stage", "message": f"LLM 응답 {len(buf)}자 수신…"}
    except Exception as e:
        yield {"event": "warn", "message": f"위키 구조 LLM 실패: {e}"}

    docs = normalize_docs(_extract_json(buf), valid_node_ids, valid_tax)
    for d in docs:
        yield {"event": "doc_proposed", "doc": d}
    if not docs:
        yield {"event": "warn", "message": "위키 구조가 비었습니다 — 모델 변경 또는 노드 확인."}
    yield {"event": "done", "docs": docs, "summary": f"위키 문서 {len(docs)}개 구조"}


_uid_counter = [0]


def _uid(prefix: str) -> str:
    _uid_counter[0] += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_uid_counter[0]}"
