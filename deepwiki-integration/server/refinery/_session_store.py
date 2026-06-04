"""[r223] refinery_sessions Supabase CRUD.

테이블 미존재 시 자동 무시 (DB_OPTIONAL_TABLES 패턴 — 사용자가 SQL 수동 실행 전).
"""
import json
import time
import re
from typing import List, Dict, Any, Optional
from supabase_store import get_store
from ._author_guard import stamp_metadata, require_author, history_entry


TABLE = "refinery_sessions"


def _uid() -> str:
    return f"rfs_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


def list_sessions(project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    store = get_store()
    try:
        q = store.client.table(TABLE).select("*").order("updated_at", desc=True).limit(200)
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


def create_session(*, project_id: Optional[str], title: str, user_id: str) -> Dict[str, Any]:
    require_author(user_id, "정련 세션 생성")
    store = get_store()
    sid = _uid()
    row = stamp_metadata(
        {
            "id": sid,
            "project_id": project_id,
            "title": title or "(이름 없음)",
            "status": "draft",
            "vault_doc_ids": [],
            "nodes": [],
            "classifications": {},
            "participants": [user_id],
            "review_request_id": None,
            "generated_tree": None,
            "generated_doc_ids": [],
            "generated_wbs_ids": [],
            "generated_task_ids": [],
            "generated_issue_ids": [],
            "parent_session_id": None,
            "version_label": "v1",
        },
        user_id=user_id,
        action="session_created",
        is_new=True,
    )
    try:
        store.client.table(TABLE).insert(row).execute()
    except Exception as e:
        # 테이블 미존재 시 메모리 라이프타임 fallback 안 함 — 사용자가 SQL 실행하도록 안내
        raise RuntimeError(
            f"refinery_sessions 테이블 미존재. 설정→DB 스키마 점검 SQL 실행 필요. ({e})"
        )
    return row


def update_session(sid: str, patch: Dict[str, Any], *, user_id: str, action: str, detail: str = "") -> Dict[str, Any]:
    require_author(user_id, "세션 수정")
    cur = get_session(sid)
    if not cur:
        raise RuntimeError(f"세션 {sid} 없음")
    # participants 누락 시 자동 추가
    parts = cur.get("participants") or []
    if user_id not in parts:
        parts.append(user_id)
        patch.setdefault("participants", parts)
    merged = {**cur, **patch}
    merged = stamp_metadata(merged, user_id=user_id, action=action, detail=detail)
    store = get_store()
    try:
        store.client.table(TABLE).update(merged).eq("id", sid).execute()
    except Exception as e:
        raise RuntimeError(f"세션 업데이트 실패: {e}")
    return merged


def delete_session(sid: str, *, user_id: str) -> bool:
    require_author(user_id, "세션 삭제")
    store = get_store()
    try:
        store.client.table(TABLE).delete().eq("id", sid).execute()
        return True
    except Exception:
        return False


# DB_OPTIONAL_TABLES 패턴 — 사용자 SQL
SCHEMA_SQL = """
-- [r223] 연구 정련소 세션
create table if not exists refinery_sessions (
  id text primary key,
  project_id text,
  title text not null,
  created_by text not null,
  updated_by text,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  status text default 'draft',
  vault_doc_ids jsonb default '[]'::jsonb,
  nodes jsonb default '[]'::jsonb,
  classifications jsonb default '{}'::jsonb,
  participants jsonb default '[]'::jsonb,
  review_request_id text,
  generated_tree jsonb,
  generated_doc_ids jsonb default '[]'::jsonb,
  generated_wbs_ids jsonb default '[]'::jsonb,
  generated_task_ids jsonb default '[]'::jsonb,
  generated_issue_ids jsonb default '[]'::jsonb,
  parent_session_id text,
  version_label text default 'v1',
  history jsonb default '[]'::jsonb
);
-- 작성자 NOT NULL 강제 (기존 데이터 백필 후)
-- alter table wiki_docs alter column created_by set not null; -- 기존 NULL 있으면 마이그레이션 도구로
"""
