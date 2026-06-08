"""[r242] meeting_sessions Supabase CRUD.

테이블 미존재 시 graceful (DB_OPTIONAL 패턴 — 사용자가 SQL 수동 실행 전).
"""
import time
from typing import List, Dict, Any, Optional
from supabase_store import get_store

TABLE = "meeting_sessions"

# refinery_sessions 와 동일 — 실제 컬럼만 통과(PostgREST 가 미정의 컬럼 섞인 write 전체 거부 방지).
_ALLOWED_COLS = {
    "id", "project_id", "title", "status", "source", "craig_id", "started_at",
    "participants", "segments", "transcript_text", "summary", "duration_sec",
    "model", "created_by", "created_at", "updated_at", "wiki_doc_id",
    # [r276] 채팅/회의록 편집 추적
    "chat_messages",       # [{at, by, byName, text}] — 텍스트 채팅(음성과 합쳐 대화내역에 표시)
    "summary_meta",        # {created_by, created_at, updated_by, updated_at, edited(bool)}
}


def _uid() -> str:
    return f"mtg_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


def _iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _clean(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in (row or {}).items() if k in _ALLOWED_COLS}


def list_sessions(project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    store = get_store()
    try:
        # 목록은 가벼운 컬럼만(segments/transcript 제외 — 응답 비대 방지)
        cols = "id,project_id,title,status,source,craig_id,started_at,participants,duration_sec,model,created_by,created_at,updated_at,wiki_doc_id"
        q = store.client.table(TABLE).select(cols).order("created_at", desc=True).limit(200)
        if project_id:
            q = q.eq("project_id", project_id)
        return q.execute().data or []
    except Exception:
        return []


def get_session(sid: str) -> Optional[Dict[str, Any]]:
    store = get_store()
    try:
        res = store.client.table(TABLE).select("*").eq("id", sid).limit(1).execute()
        return (res.data or [None])[0]
    except Exception:
        return None


def create_session(*, project_id: Optional[str], title: str, user_id: str,
                   source: str = "craig", craig_id: Optional[str] = None,
                   started_at: Optional[str] = None) -> Dict[str, Any]:
    store = get_store()
    now = _iso()
    row = {
        "id": _uid(), "project_id": project_id, "title": title or "(제목 없음)",
        "status": "importing", "source": source, "craig_id": craig_id,
        "started_at": started_at or now,   # [r275] 사용자 지정 회의 시작 시각 우선
        "participants": [], "segments": [], "transcript_text": "",
        "summary": None, "duration_sec": 0, "model": None,
        "created_by": user_id, "created_at": now, "updated_at": now, "wiki_doc_id": None,
    }
    try:
        store.client.table(TABLE).insert(_clean(row)).execute()
    except Exception as e:
        raise RuntimeError(f"meeting_sessions 테이블 미존재. 설정→DB 스키마 점검 SQL 실행 필요. ({e})")
    return row


def update_session(sid: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    store = get_store()
    safe = _clean(patch)
    safe["updated_at"] = _iso()
    try:
        store.client.table(TABLE).update(safe).eq("id", sid).execute()
    except Exception as e:
        raise RuntimeError(f"meeting_sessions 업데이트 실패: {e}")
    cur = get_session(sid) or {}
    return cur


def delete_session(sid: str) -> bool:
    store = get_store()
    try:
        store.client.table(TABLE).delete().eq("id", sid).execute()
        return True
    except Exception:
        return False


# DB_OPTIONAL — 사용자 SQL (설정>DB 스키마 점검). 멱등.
SCHEMA_SQL = """
create table if not exists meeting_sessions (
  id text primary key,
  project_id text,
  title text not null default '(제목 없음)',
  status text default 'draft',           -- importing|transcribing|summarizing|ready|error
  source text default 'craig',
  craig_id text,
  started_at timestamptz,
  participants jsonb default '[]'::jsonb, -- [{name, track}]
  segments jsonb default '[]'::jsonb,     -- [{t,dur,speaker,text}]
  transcript_text text default '',
  summary jsonb,                          -- {tldr,agenda[],decisions[],action_items[],topics[]}
  duration_sec int default 0,
  model text,
  created_by text,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  wiki_doc_id text,
  -- [r276] 채팅/회의록 편집 추적
  chat_messages jsonb default '[]'::jsonb,  -- [{at, by, byName, text}]
  summary_meta jsonb                        -- {created_by, created_at, updated_by, updated_at, edited}
);
create index if not exists idx_meeting_sessions_project on meeting_sessions(project_id, created_at desc);
-- [r276] 기존 테이블에 컬럼 추가(멱등)
alter table meeting_sessions add column if not exists chat_messages jsonb default '[]'::jsonb;
alter table meeting_sessions add column if not exists summary_meta jsonb;
alter table meeting_sessions disable row level security;
"""
