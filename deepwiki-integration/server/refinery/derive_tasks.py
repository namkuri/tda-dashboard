"""[r251] ④-1 Task 도출 — 키워드 노드별로 '공정태그' 기준 작업 도출 (계획 §④-1, 의도 #2).

분해 Tree 의 concept 노드 각각에 대해 "이 개념을 게임으로 구현/완성하려면 어떤
공정의 어떤 작업이 필요한가?"를 LLM 에 묻고, 각 Task 에 PM_TAX 공정태그 id 를 부착.
기존 프로젝트 맥락(context)을 함께 줘 중복 작업은 생략하게 한다.

산출 Task: {id,title,desc,node_id(출처 키워드),process_tag(PM_TAX id),est_hours,deliverable_kind}
이후 ④-2(의존도)·④-3(Sprint)·④-4(STAGE)·④-5(WBS) 로 이어지는 Stream 의 출발점.
"""
import time
from typing import AsyncIterator, Dict, Any, List, Optional

from llm_router import get_llm
from .intake import _extract_json  # 코드펜스/trailing-comma 복구 파서 재사용


_SYSTEM = """당신은 게임 기획/연구 개념을 '구현 작업(Task)'으로 분해하는 도구입니다.
주어진 키워드 노드 각각에 대해, 그 개념을 실제 게임으로 만들기 위해 필요한 작업을 도출합니다.
한국어로 작성하고 아래 JSON 만 출력합니다.

[규칙]
1. 각 Task 는 반드시 제공된 '공정 태그(PM_TAX)' **id 중 하나**를 process_tag 로 선택(목록 밖 금지).
2. Task 는 결과물 중심, 4~16시간(하루~이틀) 단위. 너무 크면 쪼개고 너무 작으면 합침.
3. 한 노드가 여러 공정에 걸치면 공정별로 Task 를 나눈다.
4. '기존 프로젝트' 맥락에 이미 있는 작업과 중복되면 만들지 말 것.
5. node_id 는 반드시 입력에 있는 노드 id 를 그대로 사용.
6. deliverable_kind 는 code|art|design|sound|doc|qa 중 하나(공정에 맞게).
7. 출력은 아래 JSON 하나. 설명 금지.

```json
{ "tasks": [
  {"node_id":"n_x", "title":"콤보 판정 로직 구현", "desc":"...", "process_tag":"3.1.2",
   "est_hours":8, "deliverable_kind":"code"}
] }
```
"""


def _pm_tax_block(pm_tax: List[Dict[str, Any]], limit: int = 70) -> str:
    """PM_TAX → 'id: 축약 (전체)' 목록. 소분류(v==3) 우선."""
    leaves = [t for t in (pm_tax or []) if str(t.get("v")) == "3"] or (pm_tax or [])
    rows = []
    for t in leaves[:limit]:
        s = t.get("s") or ""
        l = t.get("l") or ""
        rows.append(f"{t.get('i')}: {s}" + (f" ({l})" if l and l != s else ""))
    return "\n".join(rows) if rows else "(공정 태그 미제공 — process_tag 는 빈 문자열)"


def _nodes_block(nodes: List[Dict[str, Any]], limit: int = 50) -> str:
    """concept(잎) 노드 → '[id] title — summary' 목록."""
    leaves = [n for n in nodes if n.get("kind") != "category"]
    pool = leaves or nodes
    rows = []
    for n in pool[:limit]:
        summ = (n.get("summary") or "").strip().replace("\n", " ")
        rows.append(f"[{n.get('id')}] {n.get('title','')}" + (f" — {summ[:100]}" if summ else ""))
    return "\n".join(rows)


def build_user_prompt(nodes: List[Dict[str, Any]], pm_tax: List[Dict[str, Any]], context: str) -> str:
    return (
        f"[공정 태그(PM_TAX) — process_tag 는 이 id 중에서만]\n{_pm_tax_block(pm_tax)}\n\n"
        f"[기존 프로젝트 맥락 — 중복 작업 생략 근거]\n{context or '(없음)'}\n\n"
        f"[키워드 노드 — 각 노드의 작업을 도출]\n{_nodes_block(nodes)}\n\n"
        "각 노드를 구현/완성할 Task 들을 JSON 으로 출력하세요."
    )


def normalize_tasks(parsed: Any, valid_node_ids: set, valid_tags: set) -> List[Dict[str, Any]]:
    """LLM 결과 → 검증·정규화된 task 리스트. node_id 무효면 제외, tag 무효면 빈 문자열."""
    if not isinstance(parsed, dict):
        return []
    out: List[Dict[str, Any]] = []
    for t in (parsed.get("tasks") or []):
        if not isinstance(t, dict):
            continue
        nid = t.get("node_id")
        if valid_node_ids and nid not in valid_node_ids:
            continue  # 환각 노드 제외
        tag = t.get("process_tag") or ""
        if valid_tags and tag not in valid_tags:
            tag = ""  # 목록 밖 태그는 비움(사용자가 ④ 화면에서 지정)
        try:
            hours = float(t.get("est_hours") or 6)
        except Exception:
            hours = 6.0
        hours = max(1.0, min(40.0, hours))
        out.append({
            "id": _uid("task"),
            "node_id": nid,
            "title": (t.get("title") or "작업")[:80],
            "desc": (t.get("desc") or t.get("description") or "")[:400],
            "process_tag": tag,
            "est_hours": hours,
            "deliverable_kind": (t.get("deliverable_kind") or "code")[:12],
        })
    return out


async def derive_tasks(
    *,
    nodes: List[Dict[str, Any]],
    pm_tax: Optional[List[Dict[str, Any]]] = None,
    context: str = "",
    instruction: str = "",
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """키워드 노드 → 공정태그 부착 Task 도출 (SSE: stage / task_proposed / done).

    instruction: 재도출 시 증량/압축 등 사용자 조정 지시(프롬프트에 덧붙임)."""
    llm = get_llm(model)
    leaves = [n for n in nodes if n.get("kind") != "category"]
    pool = leaves or nodes
    valid_node_ids = {n.get("id") for n in pool}
    valid_tags = {str(t.get("i")) for t in (pm_tax or [])}
    yield {"event": "stage", "message": f"키워드 {len(pool)}개 → 공정별 Task 도출 요청"}

    prompt = build_user_prompt(pool, pm_tax or [], context)
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
        yield {"event": "warn", "message": f"Task 도출 LLM 실패: {e}"}

    tasks = normalize_tasks(_extract_json(buf), valid_node_ids, valid_tags)
    for t in tasks:
        yield {"event": "task_proposed", "task": t}

    no_tag = sum(1 for t in tasks if not t["process_tag"])
    if not tasks:
        yield {"event": "warn", "message": "도출된 Task 가 없습니다 — 모델 변경 또는 노드 확인."}
    yield {
        "event": "done",
        "tasks": tasks,
        "summary": f"Task {len(tasks)}개 도출" + (f" (공정 미지정 {no_tag})" if no_tag else ""),
    }


_uid_counter = [0]


def _uid(prefix: str) -> str:
    _uid_counter[0] += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_uid_counter[0]}"
