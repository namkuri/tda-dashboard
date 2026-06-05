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
위키는 작업·구현순서·일정과 무관합니다. 게임요소를 일반적인 게임개발 분류체계에 따라
**깊은 폴더 계층**으로 나누고, 각 말단 요소가 개별적으로 잘 정의(Well-Defined)되도록
문서 '구조(목차)'만 설계합니다. 본문은 쓰지 않습니다. 한국어, JSON only.

[규칙]
1. **[분해 키워드 계층]은 이미 문서를 구조 분류한 트리다. 이를 적극 참조·심화하라.**
   거기 드러난 대분류/중분류/말단 개념을 그대로 살리되, 게임개발 위키 관점으로
   재배치하고 **더 깊게** 세분화한다.
2. 표준 분류(WIKI_TAX) 대분류를 최상위로 삼고, 그 아래로 구체 폴더를 깊게 파라.
   예) 시스템 > 플레이어 > 인벤토리 > UI > 장착 > (문서: 슬롯).
   **반드시 4단계 이상**, 가능하면 5~6단계. 2단계에서 멈추지 말 것.
3. **모든 문서를 같은 폴더에 몰아넣지 말 것.** 내용이 다르면 folder_path 도 달라야 한다.
   문서 본문 의도를 깊이 캐치해 서로 다른 적합한 분류로 흩어라.
4. folder_path: 최상위 분류부터 말단 직전까지의 폴더 배열(4~6단계).
   title: 그 폴더 안 실제 문서(말단 게임요소) 이름.
5. wiki_tax: folder_path[0] 에 해당하는 WIKI_TAX 대분류 id.
6. 내용이 있는 가지만 생성(필요한 부분만). 각 문서 outline(목차 3~8개):
   정의/규칙/파라미터/관계/예시 등 well-defined 관점.
7. origin_node_ids: 출처 키워드 노드 id. 작업·구현순서·일정·공수는 절대 금지.
8. 출력 JSON only.

```json
{ "docs":[
  {"title":"슬롯","folder_path":["시스템","플레이어","인벤토리","UI","장착"],
   "wiki_tax":"1","summary":"장착 슬롯의 정의와 규칙.",
   "outline":["정의","슬롯 종류","장착 규칙","제약 파라미터","UI 표현"],"origin_node_ids":["n_x"]},
  {"title":"콤보 판정 우선순위","folder_path":["시스템","전투","콤보","판정"],"wiki_tax":"1",
   "summary":"...","outline":["정의","입력 윈도우","판정 우선순위","캔슬 관계"],"origin_node_ids":["n_y"]}
]}
```
"""


def _tax_block(wiki_tax: List[Dict[str, Any]], limit: int = 60) -> str:
    rows = []
    for t in (wiki_tax or [])[:limit]:
        s = t.get("s") or ""
        l = t.get("l") or ""
        rows.append(f"{t.get('i')}: {s}" + (f" ({l})" if l and l != s else ""))
    return "\n".join(rows) if rows else "(표준 분류 미제공)"


def _nodes_tree(nodes: List[Dict[str, Any]], limit: int = 140) -> str:
    """[r262] 분해 노드를 parent_id 계층(들여쓰기 트리)로 — 위키 분류 심화의 참조 기반."""
    by_parent: Dict[Any, List[Dict[str, Any]]] = {}
    for n in (nodes or []):
        by_parent.setdefault(n.get("parent_id"), []).append(n)
    lines: List[str] = []

    def walk(parent, depth):
        for n in by_parent.get(parent, []):
            if len(lines) >= limit:
                return
            pad = "  " * depth
            mark = "▸" if n.get("kind") == "category" else "-"
            summ = (n.get("summary") or "").strip().replace("\n", " ")
            lines.append(f"{pad}{mark} [{n.get('id')}] {n.get('title','')}" + (f" — {summ[:60]}" if summ else ""))
            walk(n.get("id"), depth + 1)

    walk(None, 0)
    return "\n".join(lines)


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
        folder_path = [str(x)[:40] for x in (d.get("folder_path") or []) if str(x).strip()][:6]
        out.append({
            "id": _uid("wiki"),
            "title": (d.get("title") or "문서")[:120],
            "wiki_tax": tax,
            "folder_path": folder_path,
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
    instruction: str = "",
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
        f"[분해 키워드 계층 — 이미 분류된 트리. 참조·심화]\n{_nodes_tree(nodes or [])}\n\n"
        "위 키워드 계층을 참조해, 각 말단 게임요소를 4단계 이상 깊은 폴더로 흩어 "
        "위키 문서 구조(목차) JSON 으로 출력하세요. 같은 폴더에 몰지 마세요."
    )
    if instruction:
        prompt += f"\n\n[추가 지시]\n{instruction}"
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
