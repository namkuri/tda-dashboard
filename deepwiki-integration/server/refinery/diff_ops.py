"""[r223] 세션 v1 vs v2 diff + 가설→확정 promote.

diff: 추가/변경/삭제 노드 + 카테고리 변경.
promote: 가설 노드를 확정으로 → 새 결재.
"""
from typing import List, Dict, Any
from ._author_guard import history_entry, require_author


def session_diff(prev: Dict[str, Any], curr: Dict[str, Any]) -> Dict[str, Any]:
    """두 세션의 노드/분류 diff."""
    p_nodes = {n["id"]: n for n in (prev.get("nodes") or [])}
    c_nodes = {n["id"]: n for n in (curr.get("nodes") or [])}
    added = [c_nodes[k] for k in c_nodes if k not in p_nodes]
    removed = [p_nodes[k] for k in p_nodes if k not in c_nodes]
    changed = []
    for k in c_nodes:
        if k in p_nodes:
            if p_nodes[k].get("title") != c_nodes[k].get("title") or p_nodes[k].get("summary") != c_nodes[k].get("summary"):
                changed.append({"id": k, "prev": p_nodes[k], "curr": c_nodes[k]})
    p_cls = prev.get("classifications") or {}
    c_cls = curr.get("classifications") or {}
    cls_changes = []
    for nid in set(list(p_cls.keys()) + list(c_cls.keys())):
        if p_cls.get(nid) != c_cls.get(nid):
            cls_changes.append({"node_id": nid, "from": p_cls.get(nid), "to": c_cls.get(nid)})
    return {
        "added_nodes": added,
        "removed_nodes": removed,
        "changed_nodes": changed,
        "classification_changes": cls_changes,
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
        "cls_changes_count": len(cls_changes),
    }


def promote_hypothesis(*, session: Dict[str, Any], node_ids: List[str], user_id: str, playtest_result: str = "positive") -> Dict[str, Any]:
    """가설 → 확정 승격. classifications 변경 + history 기록."""
    require_author(user_id, "가설 승격")
    cls = session.get("classifications") or {}
    changed = []
    for nid in node_ids:
        if cls.get(nid) == "hyp":
            cls[nid] = "canon"
            changed.append(nid)
    new_hist = (session.get("history") or [])
    new_hist.append(history_entry(
        "hypothesis_promoted", user_id,
        f"{len(changed)}개 가설 → 확정 (playtest: {playtest_result})",
        node_ids=changed,
    ))
    return {
        "session_patch": {"classifications": cls, "history": new_hist},
        "promoted_count": len(changed),
        "promoted_ids": changed,
    }
