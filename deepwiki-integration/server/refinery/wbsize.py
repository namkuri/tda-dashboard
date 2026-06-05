"""[r252] ④-5 WBS 분류 + 타임라인 (계획 §④-5, 의도 #6). 로직 중심(LLM 배치는 후속).

STAGE 1개 → WBS 노드 1개(기본). 그 STAGE 의 스프린트들이 노드의 `links._segments`
막대가 된다. 막대 날짜는 start_date + (seq-1)*sprint_weeks 로 계산, sprintIds/taskIds
채움, Task 의존(depends_on)이 스프린트 경계를 넘으면 세그먼트 의존 deps[{n,s}] 로 변환.
WBS↔Sprint↔Task↔STAGE 가 하나의 Stream 으로 묶인다.
"""
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional


def _parse(iso: str) -> datetime:
    return datetime.strptime((iso or "2026-01-05")[:10], "%Y-%m-%d")


def compute_sprint_window(seq: int, start_date: str, sprint_weeks: int = 2) -> Dict[str, str]:
    """seq(1-based) → {start, due} (YYYY-MM-DD). 스프린트는 연속 배치."""
    base = _parse(start_date)
    span = max(1, int(sprint_weeks)) * 7
    start = base + timedelta(days=(seq - 1) * span)
    due = start + timedelta(days=span - 1)
    return {"start": start.strftime("%Y-%m-%d"), "due": due.strftime("%Y-%m-%d")}


def build_wbs(
    *,
    stages: List[Dict[str, Any]],
    sprints: List[Dict[str, Any]],
    tasks: List[Dict[str, Any]],
    start_date: str = "2026-01-05",
    sprint_weeks: int = 2,
    parent_id_by_stage: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """STAGE/Sprint/Task → wbs_nodes[{...,links{_segments,...}}].

    parent_id_by_stage: LLM 분류 결과(기존 WBS 부착). 없으면 최상위.
    """
    parent_id_by_stage = parent_id_by_stage or {}
    sprint_by_seq = {s.get("seq"): s for s in sprints}
    task_by_id = {t.get("id"): t for t in tasks}
    # task → sprint seq
    task_seq: Dict[str, int] = {}
    for s in sprints:
        for tid in (s.get("task_ids") or []):
            task_seq[tid] = s.get("seq")

    # seq → (wbs_id, seg_id) 전역 매핑 (의존 변환용). 먼저 id 발급.
    seq_to_loc: Dict[int, Dict[str, str]] = {}
    wbs_nodes: List[Dict[str, Any]] = []
    for st in stages:
        wbs_id = _uid("wbs")
        seqs = sorted(int(x) for x in (st.get("sprint_seqs") or []))
        segments = []
        for sq in seqs:
            seg_id = f"seg_{sq}"
            seq_to_loc[sq] = {"wbs_id": wbs_id, "seg_id": seg_id}
        node = {"_wbs_id": wbs_id, "_stage": st, "_seqs": seqs, "_segments_pending": True, "title": st.get("name") or "WBS"}
        wbs_nodes.append(node)

    # 세그먼트 본체 구성(의존 포함)
    out: List[Dict[str, Any]] = []
    for node in wbs_nodes:
        wbs_id = node["_wbs_id"]
        st = node["_stage"]
        seqs = node["_seqs"]
        segments = []
        all_task_ids: List[str] = []
        all_node_ids: List[str] = []
        total_hours = 0.0
        for sq in seqs:
            sp = sprint_by_seq.get(sq) or {}
            tids = sp.get("task_ids") or []
            all_task_ids.extend(tids)
            win = compute_sprint_window(sq, start_date, sprint_weeks)
            # 세그먼트 의존: 이 스프린트 Task 의 선행이 '다른 스프린트'면 그 세그먼트로
            deps = []
            seen = set()
            for tid in tids:
                t = task_by_id.get(tid) or {}
                if t.get("node_id"):
                    all_node_ids.append(t["node_id"])
                total_hours += float(t.get("est_hours") or 0)
                for d in (t.get("depends_on") or []):
                    dseq = task_seq.get(d)
                    if dseq and dseq != sq:
                        loc = seq_to_loc.get(dseq)
                        if loc:
                            key = (loc["wbs_id"], loc["seg_id"])
                            if key not in seen:
                                seen.add(key)
                                deps.append({"n": loc["wbs_id"], "s": loc["seg_id"]})
            segments.append({
                "id": f"seg_{sq}",
                "start": win["start"], "due": win["due"],
                "sprintIds": [],          # 실제 sprint id 는 apply 단계서 연결
                "taskIds": list(tids),
                "issueIds": [],
                "deps": deps,
                "color": None,
            })
        out.append({
            "id": wbs_id,
            "parent_id": parent_id_by_stage.get(st.get("id")) or None,
            "title": (st.get("name") or "WBS")[:60],
            "description": (st.get("goal") or "")[:400],
            "stage_seq_ref": seqs,
            "links": {
                "_segments": segments,
                "taskIds": all_task_ids,
                "sprintIds": [],
                "_estimated_days": round(total_hours / 8.0, 1),
                "node_ids": list(dict.fromkeys(all_node_ids)),
                "_stage_id": st.get("id"),
            },
        })
    return out


_uid_counter = [0]


def _uid(prefix: str) -> str:
    _uid_counter[0] += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_uid_counter[0]}"
