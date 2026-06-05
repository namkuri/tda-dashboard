"""[r253] 도출 파이프라인 오케스트레이터 (계획 Phase F). B→C→D→E 를 한 SSE 로.

키워드 노드 → (B)공정태그 Task → (C)의존 위상정렬 + 기간 Sprint 분할 →
(D)STAGE 집계 → (E)WBS+타임라인. 각 단계 산출을 *_done 이벤트로 흘리고,
마지막 done 에 전체(tasks/sprints/stages/wbs)를 담는다. 프론트 6열 Stream UI 가 소비.
"""
from typing import AsyncIterator, Dict, Any, List, Optional

from .derive_tasks import derive_tasks
from .deps import infer_tech_deps
from .order import infer_deps_from_cross_links, topo_order
from .sprintize import pack_sprints
from .stagize import stagize
from .wbsize import build_wbs
from .wiki_structure import derive_wiki_structure


async def derive_stream(
    *,
    nodes: List[Dict[str, Any]],
    cross_links: Optional[List[Dict[str, Any]]] = None,
    pm_tax: Optional[List[Dict[str, Any]]] = None,
    context: str = "",
    capacity_hours: float = 80.0,
    strict: bool = False,
    rule: str = "auto",
    start_date: str = "2026-01-05",
    sprint_weeks: int = 2,
    wiki_tax: Optional[List[Dict[str, Any]]] = None,
    with_wiki: bool = True,
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """전체 도출 파이프라인(SSE). with_wiki=True 면 위키 구조까지 연속 도출."""
    # ── B. 공정태그 Task 도출
    yield {"event": "phase", "phase": "tasks", "message": "① 공정태그 Task 도출"}
    tasks: List[Dict[str, Any]] = []
    async for ev in derive_tasks(nodes=nodes, pm_tax=pm_tax, context=context, model=model):
        if ev.get("event") == "done":
            tasks = ev.get("tasks") or []
        elif ev.get("event") in ("task_proposed", "stage", "warn"):
            yield {**ev, "phase": "tasks"}
    yield {"event": "tasks_done", "tasks": tasks, "count": len(tasks)}

    # ── C. 의존 → 순서 + Sprint 분할
    yield {"event": "phase", "phase": "order", "message": "② 기술 의존도 추론 + 스프린트 분할"}
    # [r260] 기술 의존도(LLM) — Task 간 선후관계 직접 추론 후 cross_links 보강
    tech_edges = 0
    async for ev in infer_tech_deps(tasks=tasks, model=model):
        if ev.get("event") == "done":
            tech_edges = ev.get("edges", 0)
        elif ev.get("event") in ("stage", "warn"):
            yield {**ev, "phase": "order"}
    infer_deps_from_cross_links(tasks, cross_links or [])
    if tech_edges:
        yield {"event": "stage", "phase": "order", "message": f"기술 의존 {tech_edges}건 반영"}
    ordered = topo_order(tasks)
    tasks = ordered["tasks"]
    if ordered.get("cycles"):
        yield {"event": "warn", "phase": "order", "message": f"순환 의존 {len(ordered['cycles'])}건 — 약한 간선 절단"}
    yield {"event": "order_done", "levels": ordered.get("levels", 0), "tasks": tasks}
    packed = pack_sprints(tasks, capacity_hours=capacity_hours, strict=strict)
    sprints = packed["sprints"]
    yield {"event": "sprints_done", "sprints": sprints, "count": len(sprints), "capacity_hours": packed["capacity_hours"]}

    # ── D. STAGE 집계
    yield {"event": "phase", "phase": "stages", "message": "③ STAGE 마일스톤 집계"}
    stages: List[Dict[str, Any]] = []
    async for ev in stagize(sprints=sprints, tasks=tasks, context=context, rule=rule, model=model):
        if ev.get("event") == "done":
            stages = ev.get("stages") or []
        elif ev.get("event") in ("stage_proposed", "stage", "warn"):
            yield {**ev, "phase": "stages"}
    yield {"event": "stages_done", "stages": stages, "count": len(stages)}

    # ── E. WBS + 타임라인 (순수 로직)
    yield {"event": "phase", "phase": "wbs", "message": "④ WBS + 타임라인 구성"}
    wbs = build_wbs(stages=stages, sprints=sprints, tasks=tasks, start_date=start_date, sprint_weeks=sprint_weeks)
    yield {"event": "wbs_done", "wbs": wbs, "count": len(wbs)}

    # ── F. 위키 구조 (연속 도출 — 확인·체크용). [r263]
    wiki_docs: List[Dict[str, Any]] = []
    if with_wiki:
        yield {"event": "phase", "phase": "wiki", "message": "⑤ 위키 구조 도출(연속)"}
        async for ev in derive_wiki_structure(nodes=nodes, wiki_tax=wiki_tax, context=context, model=model):
            if ev.get("event") == "done":
                wiki_docs = ev.get("docs") or []
            elif ev.get("event") in ("stage", "doc_proposed", "warn"):
                yield {**ev, "phase": "wiki"}
        yield {"event": "wiki_done", "wiki": wiki_docs, "count": len(wiki_docs)}

    yield {
        "event": "done",
        "tasks": tasks, "sprints": sprints, "stages": stages, "wbs": wbs, "wiki": wiki_docs,
        "summary": f"Task {len(tasks)} · Sprint {len(sprints)} · STAGE {len(stages)} · WBS {len(wbs)} · 위키 {len(wiki_docs)}",
    }


async def rederive_downstream(
    *,
    tasks: List[Dict[str, Any]],
    cross_links: Optional[List[Dict[str, Any]]] = None,
    context: str = "",
    capacity_hours: float = 80.0,
    strict: bool = False,
    rule: str = "auto",
    start_date: str = "2026-01-05",
    sprint_weeks: int = 2,
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """[r266] 사용자가 Task 를 편집(추가/삭제)한 뒤, 그 변경을 반영해 의존→Sprint→STAGE→WBS
    를 다시 도출(연쇄 재조정). derive_tasks 는 건너뛰고 C~E 만 재실행."""
    # 기존 depends_on 초기화 후 재추론(편집 반영)
    for t in tasks:
        t["depends_on"] = []
    yield {"event": "phase", "phase": "order", "message": "② 의존도 재추론 + 스프린트 재분할"}
    async for ev in infer_tech_deps(tasks=tasks, model=model):
        if ev.get("event") in ("stage", "warn"):
            yield {**ev, "phase": "order"}
    infer_deps_from_cross_links(tasks, cross_links or [])
    ordered = topo_order(tasks)
    tasks = ordered["tasks"]
    yield {"event": "order_done", "levels": ordered.get("levels", 0), "tasks": tasks}
    packed = pack_sprints(tasks, capacity_hours=capacity_hours, strict=strict)
    sprints = packed["sprints"]
    yield {"event": "sprints_done", "sprints": sprints, "count": len(sprints), "capacity_hours": packed["capacity_hours"]}

    yield {"event": "phase", "phase": "stages", "message": "③ STAGE 재집계"}
    stages: List[Dict[str, Any]] = []
    async for ev in stagize(sprints=sprints, tasks=tasks, context=context, rule=rule, model=model):
        if ev.get("event") == "done":
            stages = ev.get("stages") or []
        elif ev.get("event") in ("stage_proposed", "stage", "warn"):
            yield {**ev, "phase": "stages"}
    yield {"event": "stages_done", "stages": stages, "count": len(stages)}

    yield {"event": "phase", "phase": "wbs", "message": "④ WBS 재구성"}
    wbs = build_wbs(stages=stages, sprints=sprints, tasks=tasks, start_date=start_date, sprint_weeks=sprint_weeks)
    yield {"event": "wbs_done", "wbs": wbs, "count": len(wbs)}
    yield {
        "event": "done", "tasks": tasks, "sprints": sprints, "stages": stages, "wbs": wbs,
        "summary": f"재조정 — Task {len(tasks)} · Sprint {len(sprints)} · STAGE {len(stages)} · WBS {len(wbs)}",
    }


_LEVEL_HINT = {
    2: "결과를 매우 잘게·많이 늘려라(증량 강).",
    1: "결과를 더 잘게·많이 늘려라(증량).",
    0: "",
    -1: "결과를 더 크게·적게 압축하라(압축).",
    -2: "결과를 매우 크게·적게 압축하라(압축 강).",
}


async def adjust_column(
    *,
    column: str,
    level: int = 0,
    instruction: str = "",
    tasks: Optional[List[Dict[str, Any]]] = None,
    sprints: Optional[List[Dict[str, Any]]] = None,
    stages: Optional[List[Dict[str, Any]]] = None,
    nodes: Optional[List[Dict[str, Any]]] = None,
    pm_tax: Optional[List[Dict[str, Any]]] = None,
    wiki_tax: Optional[List[Dict[str, Any]]] = None,
    cross_links: Optional[List[Dict[str, Any]]] = None,
    context: str = "",
    capacity_hours: float = 80.0,
    strict: bool = False,
    rule: str = "auto",
    start_date: str = "2026-01-05",
    sprint_weeks: int = 2,
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """[r267] 한 열만 재도출(증량/압축 level + 지시). done 에 그 열의 새 항목."""
    try:
        lv = int(level)
    except Exception:
        lv = 0
    instr = (_LEVEL_HINT.get(lv, "") + " " + (instruction or "")).strip()
    tasks, sprints, stages = tasks or [], sprints or [], stages or []

    if column == "tasks":
        new_tasks: List[Dict[str, Any]] = []
        async for ev in derive_tasks(nodes=nodes or [], pm_tax=pm_tax, context=context, instruction=instr, model=model):
            if ev.get("event") == "done":
                new_tasks = ev.get("tasks") or []
            elif ev.get("event") in ("stage", "task_proposed", "warn"):
                yield ev
        yield {"event": "done", "column": "tasks", "tasks": new_tasks}
    elif column == "sprints":
        cap = max(8.0, float(capacity_hours) * (1.5 ** (-lv)))  # +level → 작은 용량 → 더 많은 스프린트
        yield {"event": "stage", "message": f"스프린트 재분할 — 용량 {round(cap)}h"}
        packed = pack_sprints(tasks, capacity_hours=cap, strict=strict)
        yield {"event": "done", "column": "sprints", "sprints": packed["sprints"]}
    elif column == "stages":
        r = rule
        if lv >= 1:
            r = "by_count:1"
        elif lv <= -1:
            r = "all_one"
        new_stages: List[Dict[str, Any]] = []
        async for ev in stagize(sprints=sprints, tasks=tasks, context=context, rule=r, instruction=instr, model=model):
            if ev.get("event") == "done":
                new_stages = ev.get("stages") or []
            elif ev.get("event") in ("stage", "stage_proposed", "warn"):
                yield ev
        yield {"event": "done", "column": "stages", "stages": new_stages}
    elif column == "wbs":
        wbs = build_wbs(stages=stages, sprints=sprints, tasks=tasks, start_date=start_date, sprint_weeks=sprint_weeks)
        yield {"event": "done", "column": "wbs", "wbs": wbs}
    elif column == "wiki":
        docs: List[Dict[str, Any]] = []
        async for ev in derive_wiki_structure(nodes=nodes or [], wiki_tax=wiki_tax, context=context, instruction=instr, model=model):
            if ev.get("event") == "done":
                docs = ev.get("docs") or []
            elif ev.get("event") in ("stage", "doc_proposed", "warn"):
                yield ev
        yield {"event": "done", "column": "wiki", "wiki": docs}
    else:
        yield {"event": "error", "message": f"알 수 없는 열: {column}"}
