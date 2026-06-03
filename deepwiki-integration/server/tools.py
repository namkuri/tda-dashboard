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
                "정적 컨텐츠(코드 파일, 위키 문서 본문, 보고서)를 벡터 의미 검색으로 찾습니다. "
                "사용자가 함수 동작, 데이터 모델 설명, 아키텍처 등 코드/문서 본문 관련 질문을 할 때 호출. "
                "주의: '문서가 뭐 있어' 같은 목록 질문에는 list_docs를 사용. 이건 의미 검색용. "
                "반환: 청크 배열 [{source_type, source_id, title, content, similarity}]"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색할 자연어 키워드 또는 식별자."},
                    "project_id": {"type": "string", "description": "프로젝트 ID (선택)."},
                    "source_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": [
                            "code", "wiki", "sprint", "task",
                            # [r208] 신규 엔티티
                            "issue", "event", "asset", "review", "bug", "wbs",
                        ]},
                        "description": (
                            "검색 대상 유형 (기본: 전부). "
                            "code/wiki=정적 문서, sprint/task=칸반, "
                            "issue/bug/review=이슈·버그·결재, event=일정, asset=에셋, "
                            "wbs=작업구조화·타임라인 세그먼트."
                        ),
                    },
                    "top_k": {"type": "integer", "description": "결과 수 (기본 6)."},
                },
                "required": ["query"],
            },
        },
    },
    # ─── [r111] 신규 도구 6종 ───
    {
        "type": "function",
        "function": {
            "name": "list_docs",
            "description": (
                "위키 문서·프로젝트 문서·개인 문서 목록을 가져옵니다. "
                "사용자가 '문서 목록', '프로젝트 위키에 뭐 있어', '최근 작성된 문서', '승인된 문서' 등을 물을 때 호출. "
                "kind='wiki'=일반 문서, 'canon'=프로젝트 위키(승인), 'diagram'=그래프. "
                "반환: 문서 메타 배열 (title, kind, parent_id, sort_order, emoji, meta, updated_at)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID."},
                    "kind": {
                        "type": "string",
                        "enum": ["wiki", "canon", "diagram"],
                        "description": "문서 종류 (선택, 미지정 시 전체).",
                    },
                    "recent_first": {"type": "boolean", "description": "최근 작성 순 정렬 (기본 true)."},
                    "limit": {"type": "integer", "description": "최대 결과 수 (기본 30)."},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reviews",
            "description": (
                "결재(리뷰) 요청 목록을 가져옵니다. "
                "사용자가 '내 결재함', '내가 결재할 문건', '결재 대기 중', '진행중인 리뷰' 등을 물을 때 호출. "
                "반환: 리뷰 배열 (title, type, status, proposer_name, expires_at, votes, target_task_id, target_doc_id)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID."},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "approved", "rejected", "expired", "closed"],
                        "description": "상태 필터 (선택).",
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
            "name": "list_calendar_events",
            "description": (
                "캘린더 일정 목록을 가져옵니다. "
                "사용자가 '이번달 일정', '오늘 일정', '다가오는 미팅', '이번 주 스케줄' 등을 물을 때 호출. "
                "반환: 일정 배열 (title, start_at, end_at, description, owner_user_id, is_public)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID (선택, 미지정 시 사용자 개인 포함 전체)."},
                    "from_date": {"type": "string", "description": "시작일 YYYY-MM-DD (선택)."},
                    "to_date": {"type": "string", "description": "종료일 YYYY-MM-DD (선택)."},
                    "limit": {"type": "integer", "description": "최대 결과 수 (기본 30)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_issues",
            "description": (
                "이슈 트래커의 이슈 목록을 가져옵니다. "
                "사용자가 '이슈', '버그', '진행 중 이슈', '내 담당 이슈' 등을 물을 때 호출. "
                "반환: 이슈 배열 (title, description, priority, status, assignee, due_date, target)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID."},
                    "status": {
                        "type": "string",
                        "enum": ["open", "in_progress", "resolved", "closed"],
                        "description": "이슈 상태 (선택).",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["p0", "p1", "p2", "p3"],
                        "description": "우선순위 (선택).",
                    },
                    "limit": {"type": "integer", "description": "최대 결과 수 (기본 30)."},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sprints",
            "description": (
                "스프린트 목록을 가져옵니다 (메타만, 카드 없음). "
                "사용자가 '지난 스프린트', '스프린트 히스토리', '예정 스프린트' 등 다수 조회를 원할 때 호출. "
                "단일 진행중 스프린트 + 카드를 보려면 get_active_sprint."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID."},
                    "status": {
                        "type": "string",
                        "enum": ["active", "closed", "planned"],
                        "description": "상태 필터 (선택).",
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
            "name": "list_users",
            "description": (
                "팀원/사용자 목록을 가져옵니다. "
                "사용자가 '팀원 누구야', '참여자 목록' 등을 물을 때 호출."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID (선택)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_info",
            "description": (
                "프로젝트의 메타데이터 — 이름, 카테고리, 참여자, 연결된 Git URL, "
                "문서/카드/스프린트/이슈 카운트 등 종합 정보. 사용자가 '프로젝트 정보', "
                "'프로젝트 개요', '연결된 Git', '프로젝트 통계' 등을 물을 때 호출."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID."},
                },
                "required": ["project_id"],
            },
        },
    },
    # ─── [r116] Phase D: Deep Wiki 자동 위키 도구 5종 ───
    {
        "type": "function",
        "function": {
            "name": "list_wiki_pages",
            "description": (
                "Deep Wiki(코드 자동 분석) 1차 위키 페이지 목록. "
                "사용자가 'Deep Wiki 페이지', '자동 위키', '코드 분석 위키' 등을 물을 때 호출. "
                "반환: 페이지 메타 (slug, title, summary, git_commit, updated_at, meta)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID."},
                    "limit": {"type": "integer", "description": "최대 결과 수 (기본 30)."},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_wiki_page",
            "description": (
                "Deep Wiki 1차 위키 페이지 한 개의 본문(마크다운) 조회. "
                "사용자가 특정 시스템(예: '전투 시스템 위키 페이지 보여줘')을 묻거나 "
                "list_wiki_pages 결과 중 하나를 자세히 봐야 할 때 호출."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID."},
                    "slug": {"type": "string", "description": "페이지 슬러그 (예: 'combat', 'managers', '_overview')."},
                },
                "required": ["project_id", "slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_wiki_pages",
            "description": (
                "Deep Wiki 1차 위키 페이지 본문에서 키워드 검색. "
                "특정 함수명·테이블명·개념이 어느 페이지에 있는지 찾을 때 호출."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID."},
                    "query": {"type": "string", "description": "검색 키워드."},
                    "limit": {"type": "integer", "description": "최대 결과 수 (기본 10)."},
                },
                "required": ["project_id", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_wiki_audits",
            "description": (
                "Deep Wiki 2차 — 기획 대조 보고서 목록. "
                "사용자가 '기획 대조', '일치도', '구현 점검', '감사 보고서' 등을 물을 때 호출. "
                "반환: 보고서 메타 (title, summary, match_score, findings, related_canons)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID."},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_wiki_audit",
            "description": (
                "단일 기획 대조 보고서의 상세 본문(매핑표·findings·결론 등). "
                "list_wiki_audits 결과 중 특정 보고서 본문이 필요할 때 호출."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID."},
                    "audit_id": {"type": "string", "description": "감사 보고서 id (dwa:projectId:canonId 형식)."},
                },
                "required": ["project_id", "audit_id"],
            },
        },
    },
    # ─── [r208] WBS / 작업구조화 / 타임라인 세그먼트 도구 ───
    {
        "type": "function",
        "function": {
            "name": "list_wbs_nodes",
            "description": (
                "작업 구조화(WBS) 트리 노드 목록. 각 노드는 다중 타임라인 세그먼트(다중 진행바)와 "
                "스프린트·태스크·이슈·에셋 연결 정보를 포함. "
                "사용자가 '작업구조화', '타임라인', '마일스톤', 'WBS', '진행 상황', '전체 일정' 등을 "
                "물을 때 호출. 반환: { title, status, progress, segments[], links{} } 배열."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "프로젝트 ID."},
                    "status": {
                        "type": "string",
                        "description": "상태 필터 (선택, 예: planning/in_progress/done).",
                    },
                    "limit": {"type": "integer", "description": "최대 결과 수 (기본 50)."},
                },
                "required": ["project_id"],
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
        # [r111] 신규 도구 6개
        if name == "list_docs":
            return await _tool_list_docs(arguments)
        if name == "list_reviews":
            return await _tool_list_reviews(arguments)
        if name == "list_calendar_events":
            return await _tool_list_calendar_events(arguments)
        if name == "list_issues":
            return await _tool_list_issues(arguments)
        if name == "list_sprints":
            return await _tool_list_sprints(arguments)
        if name == "list_users":
            return await _tool_list_users(arguments)
        if name == "get_project_info":
            return await _tool_get_project_info(arguments)
        # [r116] Phase D — Deep Wiki 도구 5종
        if name == "list_wiki_pages":
            return await _tool_list_wiki_pages(arguments)
        if name == "get_wiki_page":
            return await _tool_get_wiki_page(arguments)
        if name == "search_wiki_pages":
            return await _tool_search_wiki_pages(arguments)
        if name == "list_wiki_audits":
            return await _tool_list_wiki_audits(arguments)
        if name == "get_wiki_audit":
            return await _tool_get_wiki_audit(arguments)
        # [r208] WBS
        if name == "list_wbs_nodes":
            return await _tool_list_wbs_nodes(arguments)
        return {"ok": False, "error": f"Unknown tool: {name}"}
    except Exception as e:
        import traceback
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc().splitlines()[-3:]}


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
    # [r208] 기본을 전체 source_type 으로 — issue/wbs/event/asset/review/bug 도 포함
    source_types = args.get("source_types") or [
        "code", "wiki", "sprint", "task",
        "issue", "event", "asset", "review", "bug", "wbs",
    ]
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


# ─────────────────────────────────────────────
# [r111] 신규 도구 6종 구현
# ─────────────────────────────────────────────

async def _tool_list_docs(args: Dict[str, Any]) -> Dict[str, Any]:
    """위키 문서·프로젝트 위키·다이어그램 목록 조회."""
    project_id = args.get("project_id")
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    store = get_store()
    q = store.client.table("wiki_docs").select("*").eq("project_id", project_id)
    if args.get("kind"):
        q = q.eq("kind", args["kind"])
    # sort
    recent_first = args.get("recent_first", True)
    if recent_first:
        q = q.order("updated_at", desc=True)
    else:
        q = q.order("sort_order")
    limit = int(args.get("limit") or 30)
    q = q.limit(limit)
    res = q.execute()
    rows = res.data or []
    # 컨텐츠 미리보기만 (전체는 너무 김), 작성자 이름 join
    creator_ids: List[str] = []
    for r in rows:
        if r.get("created_by"):
            creator_ids.append(r["created_by"])
    user_map = await _resolve_users(creator_ids) if creator_ids else {}
    out = []
    for r in rows:
        meta = r.get("meta") or {}
        # 폴더 여부, 승인(approved) 여부 같은 메타 추출
        item = {
            "id": r.get("id"),
            "title": r.get("title") or "(제목 없음)",
            "kind": r.get("kind") or "wiki",
            "parent_id": r.get("parent_id"),
            "emoji": r.get("emoji") or "📄",
            "is_folder": bool(meta.get("isFolder")),
            "is_deprecated": bool(r.get("is_deprecated")),
            "is_locked": bool(r.get("is_locked")),
            "updated_at": r.get("updated_at"),
            "content_preview": (r.get("content") or "").strip()[:200],
            "content_length": len(r.get("content") or ""),
            "meta": {k: v for k, v in meta.items() if k in ("isFolder", "version", "approved", "approvedBy", "approvedAt", "official")},
        }
        if r.get("created_by"):
            item["creator"] = user_map.get(r["created_by"], {"id": r["created_by"], "name": r["created_by"][:8]})
        out.append(item)
    return {"ok": True, "result": out, "count": len(out)}


async def _tool_list_reviews(args: Dict[str, Any]) -> Dict[str, Any]:
    """결재 요청 목록 — 사용자가 처리할 리뷰."""
    project_id = args.get("project_id")
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    store = get_store()
    q = store.client.table("review_requests").select("*").eq("project_id", project_id)
    if args.get("status"):
        q = q.eq("status", args["status"])
    # 삭제된 거 제외
    try:
        q = q.eq("deleted", False)
    except Exception:
        pass  # deleted 컬럼 없으면 무시
    q = q.order("expires_at", desc=False).limit(int(args.get("limit") or 20))
    try:
        res = q.execute()
        rows = res.data or []
    except Exception as e:
        return {"ok": False, "error": f"review_requests 테이블 조회 실패: {e}"}
    # proposer/assignee user 이름 join
    uids = []
    for r in rows:
        if r.get("proposer_id"):
            uids.append(r["proposer_id"])
    user_map = await _resolve_users(uids) if uids else {}
    out = []
    for r in rows:
        out.append({
            "id": r.get("id"),
            "title": r.get("title") or "(제목 없음)",
            "type": r.get("type"),
            "status": r.get("status"),
            "proposer": user_map.get(r.get("proposer_id")) or {"name": r.get("proposer_name") or "(미상)"},
            "proposer_name": r.get("proposer_name"),
            "reason": r.get("reason"),
            "target_task_id": r.get("target_task_id"),
            "target_doc_id": r.get("target_doc_id"),
            "sprint_id": r.get("sprint_id"),
            "expires_at": r.get("expires_at"),
            "decided_at": r.get("decided_at"),
            "decision_tag": r.get("decision_tag"),
            "votes_count": len(r.get("votes") or []),
            "comments_count": len(r.get("log") or []),
        })
    return {"ok": True, "result": out, "count": len(out)}


async def _tool_list_calendar_events(args: Dict[str, Any]) -> Dict[str, Any]:
    """캘린더 일정 목록.

    [r144] project_id 필터는 lenient — 사용자 캘린더 모델상 일정의 `project_id` 는
    소속 캘린더(`calendars` 테이블)의 scope 에 따라 다음과 같이 채워짐:
      - 팀 캘린더(scope=team)      → project_id NULL  (전사 공통)
      - 개인 캘린더(scope=personal) → project_id NULL  (본인용)
      - 프로젝트 캘린더(scope=project) → project_id = 해당 프로젝트
    그래서 `.eq(project_id, X)` 만 걸면 팀/개인 캘린더 일정이 통째로 사라짐.
    → `project_id == X OR project_id IS NULL` 로 완화. 또한 응답에 cal_id 및
    `calendars` join 결과(이름·scope·색)도 같이 노출해 모델이 출처를 명시 가능.
    """
    store = get_store()
    try:
        q = store.client.table("calendar_events").select("*")
    except Exception as e:
        return {"ok": False, "error": f"calendar_events 테이블 없음: {e}"}
    project_id = args.get("project_id")
    if project_id:
        # PostgREST or() 문법: project_id.eq.X,project_id.is.null
        try:
            q = q.or_(f"project_id.eq.{project_id},project_id.is.null")
        except Exception:
            # supabase-py 옛 버전 호환: .or_() 미지원이면 그냥 필터 빼고 전체 조회
            pass
    # 날짜 범위 필터
    if args.get("from_date"):
        try:
            q = q.gte("start_at", args["from_date"])
        except Exception:
            pass
    if args.get("to_date"):
        try:
            q = q.lte("start_at", args["to_date"])
        except Exception:
            pass
    q = q.order("start_at").limit(int(args.get("limit") or 50))
    try:
        res = q.execute()
        rows = res.data or []
    except Exception as e:
        return {"ok": False, "error": f"calendar_events 조회 실패: {e}"}
    # [r144] calendars 테이블도 같이 fetch 해 cal_id → {name, scope, color} 매핑
    cal_ids = list({r.get("cal_id") for r in rows if r.get("cal_id")})
    cal_map: Dict[str, Dict[str, Any]] = {}
    if cal_ids:
        try:
            cres = (
                store.client.table("calendars")
                .select("id,title,scope,color,owner_user_id,project_id")
                .in_("id", cal_ids)
                .execute()
            )
            for c in (cres.data or []):
                cal_map[c["id"]] = {
                    "name": c.get("title") or "(이름 없음)",
                    "scope": c.get("scope") or "team",
                    "color": c.get("color") or "",
                }
        except Exception:
            pass
    # owner user 이름 join
    uids = [r["owner_user_id"] for r in rows if r.get("owner_user_id")]
    user_map = await _resolve_users(uids) if uids else {}
    out = []
    for r in rows:
        cal_meta = cal_map.get(r.get("cal_id")) or {}
        out.append({
            "id": r.get("id"),
            "title": r.get("title") or "(제목 없음)",
            "start_at": r.get("start_at"),
            "end_at": r.get("end_at"),
            "description": (r.get("description") or "")[:300],
            "owner": user_map.get(r.get("owner_user_id")) or {"name": "(미상)"},
            "is_public": bool(r.get("is_public")),
            "project_id": r.get("project_id"),
            "cal_id": r.get("cal_id"),
            "calendar_name": cal_meta.get("name"),
            "calendar_scope": cal_meta.get("scope"),  # team | personal | project
        })
    note = None
    if project_id and not out:
        note = (
            f"project_id={project_id} 또는 project_id IS NULL 범위에 일정 없음. "
            "사용자가 다른 프로젝트의 일정을 묻는지 확인하거나, from_date/to_date 범위 조정 시도."
        )
    return {"ok": True, "result": out, "count": len(out), "note": note}


async def _tool_list_issues(args: Dict[str, Any]) -> Dict[str, Any]:
    """이슈 트래커 이슈 목록."""
    project_id = args.get("project_id")
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    store = get_store()
    try:
        q = store.client.table("issues").select("*").eq("project_id", project_id)
    except Exception as e:
        return {"ok": False, "error": f"issues 테이블 조회 실패: {e}"}
    if args.get("status"):
        q = q.eq("status", args["status"])
    if args.get("priority"):
        q = q.eq("priority", args["priority"])
    q = q.order("priority").limit(int(args.get("limit") or 30))
    try:
        res = q.execute()
        rows = res.data or []
    except Exception as e:
        return {"ok": False, "error": f"issues 조회 실패: {e}"}
    uids = [r["assignee_id"] for r in rows if r.get("assignee_id")]
    user_map = await _resolve_users(uids) if uids else {}
    out = []
    for r in rows:
        out.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "description": (r.get("description") or "")[:300],
            "priority": r.get("priority"),
            "status": r.get("status"),
            "target": r.get("target"),
            "due_date": r.get("due_date"),
            "assignee": user_map.get(r.get("assignee_id")) if r.get("assignee_id") else None,
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        })
    return {"ok": True, "result": out, "count": len(out)}


async def _tool_list_sprints(args: Dict[str, Any]) -> Dict[str, Any]:
    """스프린트 목록 — 메타만 (카드 없음)."""
    project_id = args.get("project_id")
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    store = get_store()
    q = store.client.table("sprints").select("*").eq("project_id", project_id)
    if args.get("status"):
        q = q.eq("status", args["status"])
    q = q.order("start_date", desc=True).limit(int(args.get("limit") or 20))
    res = q.execute()
    rows = res.data or []
    out = []
    for r in rows:
        out.append({
            "id": r.get("id"),
            "weekLabel": r.get("week_label"),
            "goal": r.get("goal"),
            "status": r.get("status"),
            "startDate": r.get("start_date"),
            "endDate": r.get("end_date"),
            "intrusionCount": r.get("intrusion_count") or 0,
            "carryoverCount": len(r.get("carryover_from_previous") or []),
            "checklistCount": len(r.get("checklists") or []),
            "closedAt": r.get("closed_at"),
        })
    return {"ok": True, "result": out, "count": len(out)}


async def _tool_list_users(args: Dict[str, Any]) -> Dict[str, Any]:
    """팀원/사용자 목록."""
    store = get_store()
    # project_id 가 있으면 그 프로젝트 participants만, 없으면 전체
    if args.get("project_id"):
        try:
            proj_res = store.client.table("projects").select("participants").eq("id", args["project_id"]).limit(1).execute()
            participants = (proj_res.data or [{}])[0].get("participants") or []
            if participants:
                user_map = await _resolve_users(list(participants))
                return {"ok": True, "result": list(user_map.values()), "count": len(user_map)}
        except Exception:
            pass
    # 폴백: 전체 users
    try:
        res = store.client.table("users").select("id,display_name,email,status_message").limit(50).execute()
        rows = res.data or []
    except Exception as e:
        return {"ok": False, "error": f"users 조회 실패: {e}"}
    out = [
        {
            "id": u.get("id"),
            "name": u.get("display_name") or (u.get("email", "").split("@")[0] if u.get("email") else u.get("id", "")[:8]),
            "email": u.get("email"),
            "status_message": u.get("status_message"),
        }
        for u in rows
    ]
    return {"ok": True, "result": out, "count": len(out)}


async def _tool_get_project_info(args: Dict[str, Any]) -> Dict[str, Any]:
    """[r112] 프로젝트 종합 메타 정보 — 이름·Git·참여자·통계."""
    project_id = args.get("project_id")
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    store = get_store()
    # 1) projects 테이블 — 이름·카테고리·참여자
    try:
        proj_res = store.client.table("projects").select("*").eq("id", project_id).limit(1).execute()
        proj = (proj_res.data or [{}])[0]
    except Exception as e:
        return {"ok": False, "error": f"projects 조회 실패: {e}"}
    if not proj:
        return {"ok": True, "result": None, "note": "해당 project_id 가 없습니다."}
    # 2) 참여자 이름 join
    participants_obj = []
    if proj.get("participants"):
        umap = await _resolve_users(list(proj["participants"]))
        participants_obj = [umap.get(uid, {"id": uid, "name": uid[:8]}) for uid in proj["participants"]]
    # 3) Git URL — projects.git_url 또는 settings 테이블 폴백
    git_url = proj.get("git_url") or None
    if not git_url:
        try:
            s_res = store.client.table("settings").select("value").eq("key", f"git_url:{project_id}").limit(1).execute()
            if s_res.data:
                git_url = (s_res.data[0] or {}).get("value")
        except Exception:
            pass
    # 4) 통계 (count="exact" 사용)
    stats: Dict[str, int] = {}
    for table, key in [
        ("wiki_docs", "docs"),
        ("tasks", "tasks"),
        ("sprints", "sprints"),
        ("issues", "issues"),
        ("review_requests", "reviews"),
        ("kanban_categories", "categories"),
        ("calendar_events", "events"),
    ]:
        try:
            r = store.client.table(table).select("id", count="exact", head=True).eq("project_id", project_id).execute()
            stats[key] = r.count or 0
        except Exception:
            stats[key] = -1  # 테이블 없음
    return {
        "ok": True,
        "result": {
            "id": proj.get("id"),
            "name": proj.get("name") or "(이름 없음)",
            "category": proj.get("category") or "일반",
            "git_url": git_url,
            "participants": participants_obj,
            "participantCount": len(participants_obj),
            "stats": stats,
            "created_at": proj.get("created_at"),
            "updated_at": proj.get("updated_at"),
        },
    }


# ─────────────────────────────────────────────
# [r116] Phase D — Deep Wiki 도구 5종
# ─────────────────────────────────────────────

async def _tool_list_wiki_pages(args: Dict[str, Any]) -> Dict[str, Any]:
    """Deep Wiki 1차 자동 위키 페이지 목록."""
    project_id = args.get("project_id")
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    store = get_store()
    try:
        res = (
            store.client.table("deep_wiki_pages")
            .select("id,slug,title,parent_slug,sort_order,summary,git_commit,updated_at,meta")
            .eq("project_id", project_id)
            .order("sort_order")
            .limit(int(args.get("limit") or 30))
            .execute()
        )
        rows = res.data or []
        if not rows:
            return {"ok": True, "result": [], "count": 0, "note": "Deep Wiki 자동 위키 페이지가 아직 생성되지 않았습니다. 자료 → Deep Wiki → 🤖 위키 자동 생성 클릭."}
        return {"ok": True, "result": rows, "count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": f"deep_wiki_pages 조회 실패: {e}"}


async def _tool_get_wiki_page(args: Dict[str, Any]) -> Dict[str, Any]:
    """Deep Wiki 1차 페이지 본문 단건 조회."""
    project_id = args.get("project_id")
    slug = args.get("slug")
    if not project_id or not slug:
        return {"ok": False, "error": "project_id, slug required"}
    store = get_store()
    try:
        res = (
            store.client.table("deep_wiki_pages")
            .select("*")
            .eq("project_id", project_id)
            .eq("slug", slug)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return {"ok": True, "result": None, "note": f"slug='{slug}' 페이지 없음. list_wiki_pages 호출로 사용 가능한 슬러그 확인."}
        # 본문 너무 길면 컷
        page = rows[0]
        page["content"] = (page.get("content") or "")[:6000]
        return {"ok": True, "result": page}
    except Exception as e:
        return {"ok": False, "error": f"조회 실패: {e}"}


async def _tool_search_wiki_pages(args: Dict[str, Any]) -> Dict[str, Any]:
    """Deep Wiki 1차 페이지 본문에서 LIKE 키워드 검색."""
    project_id = args.get("project_id")
    query = args.get("query") or ""
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    if not query.strip():
        return {"ok": False, "error": "query required"}
    store = get_store()
    try:
        res = (
            store.client.table("deep_wiki_pages")
            .select("id,slug,title,summary,content")
            .eq("project_id", project_id)
            .ilike("content", f"%{query}%")
            .limit(int(args.get("limit") or 10))
            .execute()
        )
        rows = res.data or []
        # content는 첫 등장 부근 600자 발췌
        out = []
        for r in rows:
            content = r.get("content") or ""
            idx = content.lower().find(query.lower())
            if idx < 0:
                excerpt = content[:600]
            else:
                start = max(0, idx - 100)
                excerpt = content[start:start + 600]
            out.append({
                "slug": r.get("slug"),
                "title": r.get("title"),
                "summary": r.get("summary"),
                "excerpt": excerpt,
            })
        return {"ok": True, "result": out, "count": len(out)}
    except Exception as e:
        return {"ok": False, "error": f"검색 실패: {e}"}


async def _tool_list_wiki_audits(args: Dict[str, Any]) -> Dict[str, Any]:
    """Deep Wiki 2차 — 기획 대조 보고서 목록."""
    project_id = args.get("project_id")
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    store = get_store()
    try:
        res = (
            store.client.table("deep_wiki_audits")
            .select("id,title,summary,match_score,findings,related_pages,related_canons,updated_at")
            .eq("project_id", project_id)
            .order("match_score", desc=False)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return {"ok": True, "result": [], "count": 0, "note": "기획 대조 보고서가 아직 없습니다. 자료 → Deep Wiki → 📊 기획 대조 클릭."}
        # findings는 카운트만 LLM에 노출 (본문은 get_wiki_audit으로)
        out = []
        for r in rows:
            findings = r.get("findings") or []
            severity_counts = {"high": 0, "medium": 0, "low": 0}
            for f in findings:
                sev = f.get("severity") or "low"
                severity_counts[sev] = severity_counts.get(sev, 0) + 1
            out.append({
                "id": r.get("id"),
                "title": r.get("title"),
                "summary": r.get("summary"),
                "match_score": r.get("match_score"),
                "findings_count": len(findings),
                "severity_counts": severity_counts,
                "related_pages": r.get("related_pages"),
                "updated_at": r.get("updated_at"),
            })
        return {"ok": True, "result": out, "count": len(out)}
    except Exception as e:
        return {"ok": False, "error": f"deep_wiki_audits 조회 실패: {e}"}


async def _tool_get_wiki_audit(args: Dict[str, Any]) -> Dict[str, Any]:
    """단일 기획 대조 보고서 상세."""
    project_id = args.get("project_id")
    audit_id = args.get("audit_id")
    if not project_id or not audit_id:
        return {"ok": False, "error": "project_id, audit_id required"}
    store = get_store()
    try:
        res = (
            store.client.table("deep_wiki_audits")
            .select("*")
            .eq("project_id", project_id)
            .eq("id", audit_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return {"ok": True, "result": None, "note": f"audit '{audit_id}' 없음"}
        audit = rows[0]
        # 본문 너무 길면 컷
        audit["content"] = (audit.get("content") or "")[:6000]
        return {"ok": True, "result": audit}
    except Exception as e:
        return {"ok": False, "error": f"조회 실패: {e}"}


# ─────────────────────────────────────────────
# [r208] WBS / 작업구조화 노드 + 타임라인 세그먼트
# ─────────────────────────────────────────────

async def _tool_list_wbs_nodes(args: Dict[str, Any]) -> Dict[str, Any]:
    """작업구조화 노드 목록 — segments(다중 진행바) + links(연결) 포함."""
    project_id = args.get("project_id")
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    store = get_store()
    try:
        q = (
            store.client.table("wbs_nodes")
            .select("id,title,description,status,progress,parent_id,assignees,links,sort_order,updated_at")
            .eq("project_id", project_id)
        )
        if args.get("status"):
            q = q.eq("status", args["status"])
        q = q.order("sort_order").limit(int(args.get("limit") or 50))
        res = q.execute()
        rows = res.data or []
    except Exception as e:
        return {"ok": False, "error": f"wbs_nodes 조회 실패: {e}"}

    # 담당자 이름 join
    uids: List[str] = []
    for r in rows:
        for u in (r.get("assignees") or []):
            if isinstance(u, str):
                uids.append(u)
    user_map = await _resolve_users(uids) if uids else {}

    out = []
    for r in rows:
        links = r.get("links") or {}
        segments_raw = links.get("_segments") if isinstance(links, dict) else None
        segments = []
        if isinstance(segments_raw, list):
            for s in segments_raw:
                if not isinstance(s, dict):
                    continue
                segments.append({
                    "id": s.get("id"),
                    "start": s.get("start"),
                    "due": s.get("due"),
                    "color": s.get("color"),
                    "sprintIds": s.get("sprintIds") or [],
                    "taskIds": s.get("taskIds") or [],
                    "issueIds": s.get("issueIds") or [],
                    "deps": s.get("deps") or [],
                })
        assignees_obj = []
        for uid in (r.get("assignees") or []):
            if isinstance(uid, str):
                assignees_obj.append(user_map.get(uid, {"id": uid, "name": uid[:8]}))
        out.append({
            "id": r.get("id"),
            "title": r.get("title") or "(제목 없음)",
            "description": (r.get("description") or "")[:300],
            "status": r.get("status"),
            "progress": r.get("progress") or 0,
            "parent_id": r.get("parent_id"),
            "start": (links.get("_start") if isinstance(links, dict) else None),
            "due": (links.get("_due") if isinstance(links, dict) else None),
            "segments": segments,
            "segment_count": len(segments),
            "assignees": assignees_obj,
            "linked_sprints": (links.get("sprintIds") if isinstance(links, dict) else []) or [],
            "linked_tasks": (links.get("taskIds") if isinstance(links, dict) else []) or [],
            "linked_assets": (links.get("assetIds") if isinstance(links, dict) else []) or [],
            "updated_at": r.get("updated_at"),
        })
    return {"ok": True, "result": out, "count": len(out)}
