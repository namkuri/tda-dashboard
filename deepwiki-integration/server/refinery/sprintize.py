"""[r251] ④-3 순서+공수 → Sprint 분할 (계획 §④-3, 의도 #4). 순수 로직 — LLM 불요.

위상정렬된 Task 를 순서대로 훑으며 '누적 공수 ≤ 용량'으로 그리디 패킹한다.
Task 가 topo 순서라 선행 Task 는 항상 같은/이전 스프린트에 먼저 배정 → 비-strict
모드에서 의존성은 자동 충족. strict 모드는 선행과 다른(이후) 스프린트를 강제한다.

용량 기본값: 2주 × 40h/주 = 80h/스프린트 (가변 — 호출부가 전달).
주차 라벨/날짜는 시간 의존이라 여기서 만들지 않고 route 가 start_date 로 부여한다.
"""
import time
from typing import List, Dict, Any


def pack_sprints(
    ordered_tasks: List[Dict[str, Any]],
    capacity_hours: float = 80.0,
    strict: bool = False,
) -> Dict[str, Any]:
    """topo 순서 Task → 스프린트 묶음.

    반환: {sprints:[{id,seq,task_ids,hours}], task_sprint:{task_id: seq}}.
    """
    capacity = max(4.0, float(capacity_hours or 80.0))
    sprints: List[Dict[str, Any]] = []

    def ensure(idx: int):
        while len(sprints) <= idx:
            sprints.append({"id": _uid("sprint"), "seq": len(sprints) + 1, "task_ids": [], "hours": 0.0})

    task_sprint: Dict[str, int] = {}
    cur = 0
    ensure(0)
    for t in ordered_tasks:
        tid = t.get("id")
        hours = float(t.get("est_hours") or 6)
        dep_idx = [task_sprint[d] for d in (t.get("depends_on") or []) if d in task_sprint]
        min_idx = (max(dep_idx) + (1 if strict else 0)) if dep_idx else 0
        # 의존성으로 전진
        if cur < min_idx:
            cur = min_idx
            ensure(cur)
        # 용량 초과면 다음 스프린트(현재 스프린트가 비어있지 않을 때만)
        if sprints[cur]["hours"] > 0 and sprints[cur]["hours"] + hours > capacity:
            cur += 1
            ensure(cur)
            if cur < min_idx:
                cur = min_idx
                ensure(cur)
        sprints[cur]["task_ids"].append(tid)
        sprints[cur]["hours"] += hours
        task_sprint[tid] = cur

    # seq 는 1-based, idx→seq 매핑 정리
    for i, s in enumerate(sprints):
        s["seq"] = i + 1
    task_sprint_seq = {tid: idx + 1 for tid, idx in task_sprint.items()}
    return {"sprints": sprints, "task_sprint": task_sprint_seq, "capacity_hours": capacity}


_uid_counter = [0]


def _uid(prefix: str) -> str:
    _uid_counter[0] += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_uid_counter[0]}"
