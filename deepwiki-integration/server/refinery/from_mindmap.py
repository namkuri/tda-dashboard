"""[r250] 마인드맵 추출기 결과 → 정련소 노드 변환 (결정 #1, 계획 §④/③ 재사용).

마인드맵(`/mindmap/generate`)의 branches_raw 트리:
  {central, branches:[{title, origin:[문서], children:[...]}], cross_links:[{from,to,label}]}
를 정련소 노드 형식으로 변환한다. 이렇게 하면 cross_links(개념 간 관계)·다중문서
클러스터링·메타누출필터의 이점을 정련소 도출 파이프라인이 그대로 흡수한다.

정련소 노드: {id,title,kind(category|concept),summary,parent_id,ai_confidence,
              level,auto_category_hint,source_refs:[{vault_title,span_text}]}
cross_links 는 노드 id 로 해석해 함께 반환 → ④-2 의존도 단계의 힌트로 사용.
"""
import time
from typing import List, Dict, Any, Optional

_uid_counter = [0]


def _uid(prefix: str = "n") -> str:
    _uid_counter[0] += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_uid_counter[0]}"


def branches_to_nodes(
    *,
    central: str,
    branches: List[Dict[str, Any]],
    cross_links: Optional[List[Dict[str, Any]]] = None,
    make_root: bool = True,
) -> Dict[str, Any]:
    """마인드맵 트리 → {nodes, cross_links, central_id}.

    make_root=True 이면 central 을 최상위 category 노드로 만들고 1레벨 branch 가
    그 자식이 된다. False 면 1레벨 branch 들이 각각 루트(parent_id=None).
    """
    nodes: List[Dict[str, Any]] = []
    title_to_id: Dict[str, str] = {}

    central = (central or "마인드맵").strip()
    root_id: Optional[str] = None
    if make_root:
        root_id = _uid("n")
        nodes.append({
            "id": root_id, "title": central[:60], "kind": "category", "summary": "",
            "parent_id": None, "ai_confidence": 80, "level": 0,
            "auto_category_hint": "canon",
            "source_refs": [],
        })
        title_to_id.setdefault(central, root_id)

    def walk(branch: Dict[str, Any], parent_id: Optional[str], level: int):
        if not isinstance(branch, dict):
            return
        title = (branch.get("title") or "").strip()
        if not title:
            return
        kids = branch.get("children") or []
        kind = "category" if kids else "concept"
        nid = _uid("n")
        origin = branch.get("origin") or []
        refs = [{"vault_title": str(o)[:80], "span_text": ""} for o in origin][:8]
        nodes.append({
            "id": nid, "title": title[:60], "kind": kind, "summary": "",
            "parent_id": parent_id, "ai_confidence": 75 if kind == "category" else 70,
            "level": level, "auto_category_hint": "canon",
            "source_refs": refs,
        })
        # 제목→id (cross_link 해석용, 첫 등장 우선)
        title_to_id.setdefault(title, nid)
        for ch in kids:
            walk(ch, nid, level + 1)

    for br in (branches or []):
        walk(br, root_id, 1 if make_root else 0)

    # cross_links 를 노드 id 로 해석 (제목 매칭). 매칭 안 되면 제외.
    resolved: List[Dict[str, Any]] = []
    for cl in (cross_links or []):
        if not isinstance(cl, dict):
            continue
        a = title_to_id.get((cl.get("from") or "").strip())
        b = title_to_id.get((cl.get("to") or "").strip())
        if a and b and a != b:
            resolved.append({"from": a, "to": b, "label": (cl.get("label") or "")[:60]})

    return {"nodes": nodes, "cross_links": resolved, "central_id": root_id}
