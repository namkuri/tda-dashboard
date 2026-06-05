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
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """전체 도출 파이프라인(SSE)."""
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

    yield {
        "event": "done",
        "tasks": tasks, "sprints": sprints, "stages": stages, "wbs": wbs,
        "summary": f"Task {len(tasks)} · Sprint {len(sprints)} · STAGE {len(stages)} · WBS {len(wbs)}",
    }
