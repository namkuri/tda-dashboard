"""[r251] ④-2 의존도 → 진행순서 (계획 §④-2, 의도 #3). 순수 로직 — LLM 불요.

마인드맵 cross_links(개념 관계) 또는 LLM 이 채운 task.depends_on 으로 Task 간
선후 관계를 만들고, 위상정렬(Kahn)로 선형 순서 + 병렬 가능 레벨을 계산한다.
순환은 약한 간선을 끊어 깨고 경고로 남긴다.
"""
from typing import List, Dict, Any, Optional, Tuple


def infer_deps_from_cross_links(
    tasks: List[Dict[str, Any]],
    cross_links: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """노드 관계(cross_links: [{from,to}] 노드 id)를 Task 의존으로 투영.

    from 노드의 Task 들이 to 노드의 Task 들보다 선행이라고 본다(휴리스틱).
    tasks 의 depends_on 에 task id 를 누적(중복 제거). tasks 를 그대로 반환(in-place).
    """
    by_node: Dict[Any, List[str]] = {}
    for t in tasks:
        by_node.setdefault(t.get("node_id"), []).append(t["id"])
    for cl in (cross_links or []):
        a, b = cl.get("from"), cl.get("to")
        if a == b:
            continue
        pres = by_node.get(a) or []
        for t in tasks:
            if t.get("node_id") == b and pres:
                dep = t.setdefault("depends_on", [])
                for p in pres:
                    if p not in dep and p != t["id"]:
                        dep.append(p)
    return tasks


def topo_order(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """task.depends_on(선행 task id) 기준 위상정렬.

    반환: {tasks(order·level 부여, 순서대로), levels(병렬 그룹 수), cycles(끊은 간선)}.
    Kahn 알고리즘 + 안정 정렬(원래 순서 보존). 순환은 진입차수 최소 노드를 강제 투입.
    """
    ids = [t["id"] for t in tasks]
    idset = set(ids)
    by_id = {t["id"]: t for t in tasks}
    # 유효 간선만(존재하는 task 참조, 자기참조 제외)
    deps: Dict[str, set] = {}
    for t in tasks:
        d = {x for x in (t.get("depends_on") or []) if x in idset and x != t["id"]}
        deps[t["id"]] = d
    indeg = {i: len(deps[i]) for i in ids}
    # 후행 목록
    nexts: Dict[str, List[str]] = {i: [] for i in ids}
    for i in ids:
        for d in deps[i]:
            nexts[d].append(i)

    ordered: List[str] = []
    level_of: Dict[str, int] = {}
    cycles: List[str] = []
    remaining = set(ids)
    # 레벨 단위 처리(같은 레벨=병렬 가능)
    level = 0
    frontier = [i for i in ids if indeg[i] == 0]  # 원래 순서 보존
    while remaining:
        if not frontier:
            # 순환 — 남은 것 중 진입차수 최소를 강제(원래 순서 우선)
            cand = min(remaining, key=lambda i: (indeg[i], ids.index(i)))
            cycles.append(cand)
            frontier = [cand]
        nxt: List[str] = []
        for i in frontier:
            if i not in remaining:
                continue
            ordered.append(i)
            level_of[i] = level
            remaining.discard(i)
            for j in nexts[i]:
                if j in remaining:
                    indeg[j] -= 1
                    if indeg[j] <= 0 and j not in nxt:
                        nxt.append(j)
        # 다음 프런티어(원래 순서 정렬)
        frontier = sorted([j for j in nxt if j in remaining], key=lambda i: ids.index(i))
        level += 1

    out_tasks = []
    for k, i in enumerate(ordered):
        t = dict(by_id[i])
        t["order"] = k
        t["level"] = level_of.get(i, 0)
        out_tasks.append(t)
    return {
        "tasks": out_tasks,
        "levels": (max(level_of.values()) + 1) if level_of else 0,
        "cycles": cycles,
    }
