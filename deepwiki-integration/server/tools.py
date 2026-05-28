"""[r108→r110] Tool Use 도구 정의 + 실행기.

LLM이 사용자 질문을 분석해 어떤 도구를 호출할지 결정한다.
각 도구는 Supabase에서 직접 데이터를 조회하거나, 기존 벡터 검색을 호출.

r110: 도구 결과의 user_id/cat_id를 사람이 읽을 수 있는 이름으로 자동 join.
"""
import json
from typing import Any, Dict, List, Optional
from supabase_store import get_store
from retriever import retrieve as vector_retrieve


# ─────────────────────────────────────────────
# [r110] ID → 이름 매핑 헬퍼
# ─────────────────────────────────────────────

async def _resolve_users(user_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """user_id 리스트 → {id: {id, name, email}} 매핑."""
    ids = [u for u in (user_ids or []) if u and isinstance(u, str)]
    if not ids:
        return {}
    store = get_store()
    try:
        res = store.client.table("users").select("id,display_name,email").in_("id", list(set(ids))).execute()
        out: Dict[str, Dict[str, Any]] = {}
        for u in (res.data or []):
            uid = u.get("id")
            out[uid] = {
                "id": uid,
                "name": u.get("display_name") or (u.get("email", "").split("@")[0] if u.get("email") else uid[:8]),
                "email": u.get("email"),
            }
        return out
    except Exception:
        return {}


async def _resolve_categories(cat_ids: List[str], project_id: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """cat_id 리스트 → {id: {id, title, subtitle}} 매핑."""
    ids = [c for c in (cat_ids or []) if c and isinstance(c, str)]
    if not ids:
        return {}
    store = get_store()
    try:
        q = store.client.table("kanban_categories").select("id,title,subtitle").in_("id", list(set(ids)))
        if project_id:
            q = q.eq("project_id", project_id)
        res = q.execute()
        return {c["id"]: {"id": c["id"], "title": c.get("title") or "(이름 없음)", "subtitle": c.get("subtitle")} for c in (res.data or [])}
    except Exception:
        return {}


def _enrich_card(card: Dict[str, Any], user_map: Dict[str, Dict], cat_map: Dict[str, Dict]) -> Dict[str, Any]:
    """카드 1개의 assignees/cat_id를 객체로 변환."""
    out = dict(card)
    raw_assignees = card.get("assignees") or []
    if isinstance(raw_assignees, list):
        out["assignees"] = [user_map.get(uid, {"id": uid, "name": uid[:8] if uid else "?"}) for uid in raw_assignees]
    if card.get("cat_id"):
        out["category"] = cat_map.get(card["cat_id"], {"id": card["cat_id"], "title": "?"})
    return out


# ─────────────────────────────────────────────
# 도구 스키마 (OpenAI Function Calling 호환)
# ─────────────────────────────────────────────

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_active_sprint",
            "description": (
                "현재 진행중인(status='active') 스프린트의 상세 정보를 가져옵니다. "
                "사용자가 '진행중인 스프린트', '이번 스프린트', '현재 활성 스프린트' 등을 물을 때 호출하세요. "
                "반환: { id, weekLabel, goal, status, startDate, endDate, intrusionCount, participants, cards: [{title, status, zone, dueDate, assignees}, ...] }"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "프로젝트 ID. 사용자 컨텍스트에서 자동 주입됨.",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": (
                "필터링된 칸반 카드(태스크) 목록을 가져옵니다. "
                "사용자가 '내 카드', '마감 임박', '미완료 태스크', 'Now 존 카드' 등 동적 필터링을 묻을 때 호출. "
                "반환: 카드 배열 [{id, title, description, status, zone, priority, sprint_id, due_date, assignees, cat_id}]"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID (필수)."},
                    "sprint_id": {"type": "string", "description": "특정 스프린트 ID로 필터링 (선택)."},
                    "zone": {
                        "type": "string",
                        "enum": ["now", "shelf", "buried"],
                        "description": "Zone 필터: now=현재, shelf=대기, buried=묻힘 (선택).",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "progress", "completed"],
                        "description": "상태 필터 (선택).",
                    },
                    "due_before_days": {
                        "type": "integer",
                        "description": "오늘 기준 N일 이내 마감만 (마감 임박 검색용, 선택).",
                    },
                    "limit": {"type": "integer", "description": "최대 결과 수 (기본 20)."},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_vector",
            "description": (
                "정적 컨텐츠(코드 파일, 위키 문서, 보고서)를 벡터 의미 검색으로 찾습니다. "
                "사용자가 함수 동작, 데이터 모델 설명, 아키텍처 등 코드/문서 관련 질문을 할 때 호출. "
                "반환: 청크 배열 [{source_type, source_id, title, content, similarity}]"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색할 자연어 키워드 또는 식별자."},
                    "project_id": {"type": "string", "description": "프로젝트 ID (선택)."},
                    "source_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["code", "wiki"]},
                        "description": "검색 대상 유형 (기본: code+wiki).",
                    },
                    "top_k": {"type": "integer", "description": "결과 수 (기본 6)."},
                },
                "required": ["query"],
            },
        },
    },
]


# ─────────────────────────────────────────────
# 실행기 — 각 도구 구현
# ─────────────────────────────────────────────

async def execute_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """LLM이 호출 결정한 도구를 실제 실행하고 결과 반환.

    Returns: { ok: bool, result: any, error?: str }
    """
    try:
        if name == "get_active_sprint":
            return await _tool_get_active_sprint(arguments)
        if name == "list_tasks":
            return await _tool_list_tasks(arguments)
        if name == "search_vector":
            return await _tool_search_vector(arguments)
        return {"ok": False, "error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _tool_get_active_sprint(args: Dict[str, Any]) -> Dict[str, Any]:
    """진행중 스프린트 + 그 안의 카드들 조회. [r110] user/category 이름 자동 join."""
    project_id = args.get("project_id")
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    store = get_store()
    # 활성 스프린트 (가장 최근 startDate)
    res = (
        store.client.table("sprints")
        .select("*")
        .eq("project_id", project_id)
        .eq("status", "active")
        .order("start_date", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return {"ok": True, "result": None, "note": "현재 active 상태인 스프린트가 없습니다."}
    sp = rows[0]
    # 그 스프린트의 카드들
    tasks_res = (
        store.client.table("tasks")
        .select("id,title,description,status,zone,priority,due_date,assignees,cat_id,carryover_count,is_starred")
        .eq("project_id", project_id)
        .eq("sprint_id", sp["id"])
        .limit(50)
        .execute()
    )
    cards = tasks_res.data or []
    # 카테고리(컬럼)
    cats_res = (
        store.client.table("kanban_categories")
        .select("id,title,subtitle")
        .eq("project_id", project_id)
        .eq("sprint_id", sp["id"])
        .execute()
    )
    cats = cats_res.data or []
    # [r110] 모든 user_id 모아서 한 번에 join
    all_uids: List[str] = list(sp.get("participants") or [])
    for c in cards:
        for u in (c.get("assignees") or []):
            if isinstance(u, str):
                all_uids.append(u)
    user_map = await _resolve_users(all_uids)
    # 카테고리 매핑 (kbox 안에 이미 있음)
    cat_map = {c["id"]: {"id": c["id"], "title": c.get("title") or "?", "subtitle": c.get("subtitle")} for c in cats}
    # 카드들에 이름 join
    enriched_cards = [_enrich_card(c, user_map, cat_map) for c in cards]
    # 참여자 객체화
    participants_obj = [user_map.get(uid, {"id": uid, "name": uid[:8] if uid else "?"}) for uid in (sp.get("participants") or [])]
    return {
        "ok": True,
        "result": {
            "id": sp.get("id"),
            "weekLabel": sp.get("week_label"),
            "goal": sp.get("goal"),
            "status": sp.get("status"),
            "startDate": sp.get("start_date"),
            "endDate": sp.get("end_date"),
            "intrusionCount": sp.get("intrusion_count", 0),
            "participants": participants_obj,
            "categories": list(cat_map.values()),
            "cards": enriched_cards,
            "cardCount": len(enriched_cards),
        },
    }


async def _tool_list_tasks(args: Dict[str, Any]) -> Dict[str, Any]:
    """필터링된 카드 목록 조회. [r110] user/category 이름 자동 join."""
    project_id = args.get("project_id")
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    store = get_store()
    q = (
        store.client.table("tasks")
        .select("id,title,description,status,zone,priority,sprint_id,due_date,assignees,cat_id,is_starred,carryover_count")
        .eq("project_id", project_id)
    )
    if args.get("sprint_id"):
        q = q.eq("sprint_id", args["sprint_id"])
    if args.get("zone"):
        q = q.eq("zone", args["zone"])
    if args.get("status"):
        q = q.eq("status", args["status"])
    limit = args.get("limit") or 20
    q = q.limit(int(limit) * 2)
    res = q.execute()
    rows = res.data or []
    if args.get("due_before_days") is not None:
        from datetime import date, timedelta
        cutoff = date.today() + timedelta(days=int(args["due_before_days"]))
        cutoff_iso = cutoff.isoformat()
        rows = [r for r in rows if r.get("due_date") and r["due_date"] <= cutoff_iso]
    rows = rows[:int(limit)]
    # [r110] user / category 매핑
    all_uids: List[str] = []
    all_cids: List[str] = []
    for r in rows:
        for u in (r.get("assignees") or []):
            if isinstance(u, str):
                all_uids.append(u)
        if r.get("cat_id"):
            all_cids.append(r["cat_id"])
    user_map = await _resolve_users(all_uids)
    cat_map = await _resolve_categories(all_cids, project_id)
    enriched = [_enrich_card(r, user_map, cat_map) for r in rows]
    return {"ok": True, "result": enriched, "count": len(enriched)}


async def _tool_search_vector(args: Dict[str, Any]) -> Dict[str, Any]:
    """기존 RAG 벡터 검색을 도구로 래핑."""
    query = args.get("query") or ""
    if not query.strip():
        return {"ok": False, "error": "query required"}
    project_id = args.get("project_id")
    source_types = args.get("source_types") or ["code", "wiki"]
    top_k = args.get("top_k") or 6
    chunks = await vector_retrieve(
        query=query,
        project_id=project_id,
        source_types=source_types,
        top_k=int(top_k),
    )
    # LLM에 전달용 요약 (content 1200자 컷)
    out = []
    for c in chunks:
        out.append({
            "source_type": c.get("source_type"),
            "source_id": c.get("source_id"),
            "title": c.get("source_title"),
            "similarity": round(c.get("similarity", 0), 3),
            "content": (c.get("content") or "")[:1200],
        })
    return {"ok": True, "result": out, "count": len(out)}
