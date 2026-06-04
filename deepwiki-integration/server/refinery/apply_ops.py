"""[r223] 위키/문서 트리 commit + WBS/태스크/이슈 생성.

apply_tree:
  - files[] 받아 wiki_docs 에 일괄 insert/update
  - 기존 동일 경로면 archived_versions 보존 후 덮어쓰기
  - target_kind 별 분기 (canon|wiki|personal)
  - 모든 신규 doc 에 created_by NOT NULL 강제

apply_work:
  - WBS/태스크/이슈 일괄 생성
  - 각 항목에 source_session_id 역추적
"""
import json
import time
from typing import List, Dict, Any, Optional
from supabase_store import get_store
from ._author_guard import require_author, history_entry, stamp_metadata
from .linker import extract_user_sections, reinject_user_sections


def _doc_id() -> str:
    return f"doc_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


def _wbs_id() -> str:
    return f"wbs_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


def _task_id() -> str:
    return f"task_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


def _issue_id() -> str:
    return f"issue_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


def apply_tree(
    *,
    session: Dict[str, Any],
    files: List[Dict[str, Any]],  # [{path, body, target_kind, category, node_ids, visibility?}]
    user_id: str,
    project_id: Optional[str] = None,
    create_archive: bool = True,
) -> Dict[str, Any]:
    """일괄 commit. files 중 선택된 것만 commit (체크박스는 프론트가 필터링).

    응답: {created:[], updated:[], skipped:[], warnings:[]}
    """
    require_author(user_id, "위키 트리 적용")
    store = get_store()
    created = []
    updated = []
    warnings = []
    session_id = session.get("id")
    root = (session.get("generated_tree") or {}).get("root") or f"🔬 {session.get('title')} 시스템 정의서"

    # 폴더 경로 자동 생성 — wiki_docs.kind='wiki' + meta.isFolder=true
    folder_cache: Dict[str, str] = {}  # 'root/0.정체성' → doc_id

    def ensure_folder(target_kind: str, project_id_local: Optional[str], folder_path: str) -> Optional[str]:
        if not folder_path:
            return None
        cache_key = f"{target_kind}::{project_id_local}::{folder_path}"
        if cache_key in folder_cache:
            return folder_cache[cache_key]
        # 상위 부모 먼저
        parts = folder_path.split("/")
        parent_id = None
        for i, part in enumerate(parts):
            sub_path = "/".join(parts[: i + 1])
            sub_key = f"{target_kind}::{project_id_local}::{sub_path}"
            if sub_key in folder_cache:
                parent_id = folder_cache[sub_key]
                continue
            # 기존 폴더 검색
            try:
                q = store.client.table("wiki_docs").select("id").eq("title", part).eq("kind", target_kind)
                if project_id_local:
                    q = q.eq("project_id", project_id_local)
                if parent_id:
                    q = q.eq("parent_id", parent_id)
                res = q.limit(1).execute()
                existing = (res.data or [])
                if existing:
                    folder_cache[sub_key] = existing[0]["id"]
                    parent_id = existing[0]["id"]
                    continue
            except Exception:
                pass
            # 신규 폴더 생성
            new_folder_id = _doc_id()
            folder_row = stamp_metadata(
                {
                    "id": new_folder_id,
                    "project_id": project_id_local,
                    "title": part,
                    "kind": target_kind,
                    "parent_id": parent_id,
                    "emoji": "📁",
                    "content": "",
                    "meta": {
                        "isFolder": True,
                        "refinerySessionId": session_id,
                    },
                    "sort_order": 0,
                },
                user_id=user_id,
                action="folder_created",
                is_new=True,
                detail=f"정련소 세션 #{session_id}",
            )
            try:
                store.client.table("wiki_docs").insert(folder_row).execute()
                folder_cache[sub_key] = new_folder_id
                parent_id = new_folder_id
            except Exception as e:
                warnings.append(f"폴더 생성 실패 ({sub_path}): {e}")
                return None
        return parent_id

    for f in files:
        path = f.get("path") or ""
        if not path:
            continue
        target_kind = (f.get("target_kind") or "canon").lower()
        if target_kind not in ("canon", "wiki", "personal"):
            target_kind = "canon"
        visibility = (f.get("visibility") or "public").lower()
        body = f.get("body") or ""
        # 폴더 경로 (마지막 '/' 이전)
        if "/" in path:
            folder_path = root + "/" + "/".join(path.split("/")[:-1])
            filename = path.split("/")[-1]
        else:
            folder_path = root
            filename = path
        title = filename.replace(".md", "").lstrip()
        # 상위 폴더 보장
        parent_id = ensure_folder(target_kind, project_id, folder_path)
        # 기존 파일?
        try:
            q = store.client.table("wiki_docs").select("*").eq("title", title).eq("kind", target_kind)
            if project_id and target_kind != "personal":
                q = q.eq("project_id", project_id)
            if parent_id:
                q = q.eq("parent_id", parent_id)
            existing = (q.limit(1).execute().data or [])
        except Exception:
            existing = []
        meta_base = {
            "refinerySessionId": session_id,
            "refineryNodeIds": f.get("node_ids") or [],
            "target_kind": target_kind,
            "category": f.get("category"),
            "tags": ["refinery", f.get("category", ""), *(["overview"] if f.get("is_overview") else [])],
        }
        if target_kind == "personal":
            meta_base["owner"] = user_id
            meta_base["visibility"] = visibility
        if existing:
            ex = existing[0]
            ex_meta = ex.get("meta") or {}
            # archived_versions 보존
            if create_archive:
                arch = ex_meta.get("archived_versions") or []
                arch.append({
                    "version": len(arch) + 1,
                    "content": ex.get("content") or "",
                    "by": ex.get("updated_by") or ex.get("created_by"),
                    "at": ex.get("updated_at"),
                    "snapshot_at": _iso(),
                })
                if len(arch) > 20:
                    arch = arch[-20:]
                ex_meta["archived_versions"] = arch
            # 사람 편집 보존 (USER 마커)
            old_user_sections, _ = extract_user_sections(ex.get("content") or "")
            merged_body = reinject_user_sections(body, old_user_sections) if old_user_sections else body
            new_meta = {**ex_meta, **meta_base}
            updated_row = stamp_metadata(
                {**ex, "content": merged_body, "meta": new_meta},
                user_id=user_id,
                action="wiki_committed",
                detail=f"정련소 #{session_id} → 덮어쓰기 (archived v{len(new_meta.get('archived_versions') or [])})",
            )
            try:
                store.client.table("wiki_docs").update(updated_row).eq("id", ex["id"]).execute()
                updated.append({"id": ex["id"], "path": path, "target": target_kind, "archived_count": len(new_meta.get("archived_versions") or [])})
            except Exception as e:
                warnings.append(f"덮어쓰기 실패 {path}: {e}")
        else:
            new_id = _doc_id()
            new_row = stamp_metadata(
                {
                    "id": new_id,
                    "project_id": project_id if target_kind != "personal" else project_id,
                    "title": title,
                    "kind": target_kind,
                    "parent_id": parent_id,
                    "emoji": _emoji_for(f.get("category")),
                    "content": body,
                    "meta": meta_base,
                    "sort_order": _now_ms(),
                },
                user_id=user_id,
                action="wiki_committed",
                is_new=True,
                detail=f"정련소 #{session_id} → 신규",
            )
            try:
                store.client.table("wiki_docs").insert(new_row).execute()
                created.append({"id": new_id, "path": path, "target": target_kind})
            except Exception as e:
                warnings.append(f"생성 실패 {path}: {e}")
    return {"created": created, "updated": updated, "skipped": [], "warnings": warnings}


def apply_work(
    *,
    session: Dict[str, Any],
    wbs_nodes: List[Dict[str, Any]],
    tasks: List[Dict[str, Any]],
    issues: List[Dict[str, Any]],
    user_id: str,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """제안된 WBS/태스크/이슈 일괄 insert."""
    require_author(user_id, "작업 일괄 생성")
    store = get_store()
    session_id = session.get("id")
    created_wbs = []
    created_tasks = []
    created_issues = []
    warnings = []

    # WBS — proposed.id 를 real id 로 매핑
    wbs_id_map = {}
    for wb in wbs_nodes:
        new_id = _wbs_id()
        wbs_id_map[wb.get("id")] = new_id
        row = stamp_metadata(
            {
                "id": new_id,
                "project_id": project_id,
                "parent_id": None,
                "title": wb.get("title"),
                "description": wb.get("description"),
                "status": "todo",
                "progress": 0,
                "links": {
                    "_source_session": session_id,
                    "_estimated_days": wb.get("estimated_days"),
                    "_node_ids": wb.get("node_ids") or [],
                    "_depends_on": [wbs_id_map.get(d, d) for d in (wb.get("depends_on") or [])],
                },
                "attachments": [],
                "assignees": [],
                "sort_order": _now_ms(),
            },
            user_id=user_id,
            action="wbs_created",
            is_new=True,
            detail=f"정련소 #{session_id}",
        )
        try:
            store.client.table("wbs_nodes").insert(row).execute()
            created_wbs.append({"id": new_id, "title": wb.get("title")})
        except Exception as e:
            warnings.append(f"WBS 생성 실패: {e}")

    for t in tasks:
        new_id = _task_id()
        row = stamp_metadata(
            {
                "id": new_id,
                "project_id": project_id,
                "title": t.get("title"),
                "description": t.get("description"),
                "status": "pending",
                "zone": "shelf",
                "priority": t.get("priority", "p2"),
                "sprint_id": None,
                "due_date": None,
                "assignees": [],
                "cat_id": None,
            },
            user_id=user_id,
            action="task_created",
            is_new=True,
            detail=f"정련소 #{session_id} · WBS {wbs_id_map.get(t.get('wbs_id'), t.get('wbs_id'))}",
        )
        try:
            store.client.table("tasks").insert(row).execute()
            created_tasks.append({"id": new_id, "title": t.get("title")})
        except Exception as e:
            warnings.append(f"태스크 생성 실패: {e}")

    for iss in issues:
        new_id = _issue_id()
        row = stamp_metadata(
            {
                "id": new_id,
                "project_id": project_id,
                "title": iss.get("title"),
                "description": iss.get("description"),
                "status": iss.get("status", "open"),
                "priority": iss.get("priority", "p2"),
                "target": iss.get("target"),
                "due_date": None,
                "assignee_id": None,
            },
            user_id=user_id,
            action="issue_created",
            is_new=True,
            detail=f"정련소 #{session_id}",
        )
        try:
            store.client.table("issues").insert(row).execute()
            created_issues.append({"id": new_id, "title": iss.get("title")})
        except Exception as e:
            warnings.append(f"이슈 생성 실패: {e}")

    return {
        "wbs_created": created_wbs,
        "tasks_created": created_tasks,
        "issues_created": created_issues,
        "warnings": warnings,
    }


def _emoji_for(category: Optional[str]) -> str:
    return {
        "canon": "📖", "hyp": "🧪", "later": "📋", "cut": "❌",
        "overview": "🌟", "meta": "📚",
    }.get(category or "", "📄")


def _iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)
