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
from ._author_guard import require_author, history_entry
from .linker import extract_user_sections, reinject_user_sections
from .wbsize import compute_sprint_window  # [r253] 스프린트 날짜 계산


def _doc_id() -> str:
    return f"doc_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


def _wbs_id() -> str:
    return f"wbs_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


def _task_id() -> str:
    return f"task_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


def _issue_id() -> str:
    return f"issue_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


def _stage_id() -> str:
    return f"stage_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


def _sprint_id() -> str:
    return f"sprint_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


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

    # [r232] wiki_docs 실제 스키마: sort_order=int4(오버플로우 주의), updated_at=bigint(epoch ms),
    #   update_history(=history 아님) jsonb, created_at 컬럼 없음. stamp_metadata 금지.
    _seq = {"n": int(time.time()) % 100000}  # 작은 int4 시드 — 오버플로우 회피

    def next_sort() -> int:
        _seq["n"] += 1
        return _seq["n"]

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
            # 신규 폴더 생성 — wiki_docs 실제 컬럼만 (stamp_metadata 금지)
            new_folder_id = _doc_id()
            folder_row = {
                "id": new_folder_id,
                "project_id": project_id_local,
                "parent_id": parent_id,
                "title": part,
                "kind": target_kind,
                "emoji": "📁",
                "content": "",
                "meta": {"isFolder": True, "refinerySessionId": session_id},
                "sort_order": next_sort(),
                "is_collapsed": False,
                "is_deprecated": False,
                "doc_type": "plan",
                "linked_task_ids": [],
                "linked_doc_ids": [],
                "is_locked": False,
                "created_by": user_id,
                "updated_by": user_id,
                "update_history": [history_entry("folder_created", user_id, f"정련소 세션 #{session_id}")],
                "updated_at": _now_ms(),
            }
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
            # [r232] 변경 필드만 update — wiki_docs 실제 컬럼/타입 준수 (updated_at=bigint, update_history)
            uh = ex.get("update_history") or []
            if not isinstance(uh, list):
                uh = []
            uh.append(history_entry("wiki_committed", user_id, f"정련소 #{session_id} → 덮어쓰기 (archived v{len(new_meta.get('archived_versions') or [])})"))
            if len(uh) > 200:
                uh = uh[-200:]
            updated_row = {
                "content": merged_body,
                "meta": new_meta,
                "updated_by": user_id,
                "updated_at": _now_ms(),
                "update_history": uh,
            }
            try:
                store.client.table("wiki_docs").update(updated_row).eq("id", ex["id"]).execute()
                updated.append({"id": ex["id"], "path": path, "target": target_kind, "archived_count": len(new_meta.get("archived_versions") or [])})
            except Exception as e:
                warnings.append(f"덮어쓰기 실패 {path}: {e}")
        else:
            new_id = _doc_id()
            new_row = {  # wiki_docs 실제 컬럼만 (stamp_metadata 금지 — created_at/history 없음, updated_at=bigint)
                "id": new_id,
                "project_id": project_id,
                "parent_id": parent_id,
                "title": title,
                "kind": target_kind,
                "emoji": _emoji_for(f.get("category")),
                "content": body,
                "meta": meta_base,
                "sort_order": next_sort(),
                "is_collapsed": False,
                "is_deprecated": False,
                "doc_type": "plan",
                "linked_task_ids": [],
                "linked_doc_ids": f.get("linked_doc_ids") or [],
                "is_locked": False,
                "created_by": user_id,
                "updated_by": user_id,
                "update_history": [history_entry("wiki_committed", user_id, f"정련소 #{session_id} → 신규")],
                "updated_at": _now_ms(),
            }
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
    stages: Optional[List[Dict[str, Any]]] = None,    # [r247] 프로덕트 관리 STAGE
    sprints: Optional[List[Dict[str, Any]]] = None,   # [r247] 신규 스프린트
) -> Dict[str, Any]:
    """제안된 STAGE/스프린트/WBS/태스크/이슈 일괄 insert.

    [r232] 핵심 수정: 각 테이블 스키마에 **없는 컬럼(history/updated_by)** 을 넣으면
    PostgREST 가 insert 전체를 거부 → "모두 생성" 했는데 아무것도 안 생기던 버그.
    stamp_metadata 대신 테이블별 실제 컬럼만 명시.
    [r232] parent_id(WBS 삽입 위치)·sprint_id(태스크 스프린트) 항목별 지정 지원.
    [r247] stages → product_stages, sprints → sprints. 제안된 sprint id 를 태스크의
           sprint_id 로 연결할 수 있도록 sprint_id_map 반환에 활용.
    """
    require_author(user_id, "작업 일괄 생성")
    store = get_store()
    session_id = session.get("id")
    now = _iso()
    created_stages = []
    created_sprints = []
    created_wbs = []
    created_tasks = []
    created_issues = []
    warnings = []

    # [r247] STAGE — product_stages (id,project_id,name,type,goal,lifecycle,status,sort_order,created_by,...)
    for stg in (stages or []):
        new_id = _stage_id()
        row = {
            "id": new_id,
            "project_id": project_id,
            "name": stg.get("name") or "새 Stage",
            "type": stg.get("type") or "MVP",
            "goal": stg.get("goal") or "",
            "lifecycle": stg.get("lifecycle") or "",
            "status": "active",
            "sort_order": _now_ms(),
            "created_by": user_id,
            "created_at": now,
            "updated_at": now,
        }
        try:
            store.client.table("product_stages").insert(row).execute()
            created_stages.append({"id": new_id, "name": stg.get("name")})
        except Exception as e:
            warnings.append(f"STAGE 생성 실패({stg.get('name')}): {e}")

    # [r247] 신규 스프린트 — sprints (week_label,goal,status,...). 제안 id → 실제 id 매핑.
    sprint_id_map = {}
    for sp in (sprints or []):
        new_id = _sprint_id()
        sprint_id_map[sp.get("id")] = new_id
        row = {
            "id": new_id,
            "project_id": project_id,
            "week_label": sp.get("week_label") or sp.get("weekLabel") or new_id,
            "goal": sp.get("goal") or "",
            "status": sp.get("status") or "planning",
            "intrusion_count": 0,
            "intrusion_log": [],
            "carryover_from_previous": [],
            "milestone_criteria": [],
            "start_date": sp.get("start_date"),
            "end_date": sp.get("end_date"),
            "closed_at": None,
            "history": [],
            "participants": [],
        }
        try:
            store.client.table("sprints").insert(row).execute()
            created_sprints.append({"id": new_id, "week_label": row["week_label"]})
        except Exception as e:
            warnings.append(f"스프린트 생성 실패({row['week_label']}): {e}")

    # WBS — proposed.id 를 real id 로 매핑 (parent 가 같은 배치 내 WBS 일 수 있음)
    wbs_id_map = {}
    for wb in wbs_nodes:
        new_id = _wbs_id()
        wbs_id_map[wb.get("id")] = new_id
    for wb in wbs_nodes:
        new_id = wbs_id_map[wb.get("id")]
        # [r232] 삽입 위치 — parent_id (기존 WBS 또는 같은 배치 WBS). 매핑 우선.
        parent = wb.get("parent_id")
        parent = wbs_id_map.get(parent, parent)
        row = {  # wbs_nodes 실제 컬럼만
            "id": new_id,
            "project_id": project_id,
            "parent_id": parent,
            "title": wb.get("title") or "WBS",
            "description": wb.get("description") or "",
            "status": "todo",
            "progress": 0,
            "links": {
                "_source_session": session_id,
                "_estimated_days": wb.get("estimated_days"),
                "_node_ids": wb.get("node_ids") or [],
                "_depends_on": [wbs_id_map.get(d, d) for d in (wb.get("depends_on") or [])],
                "_doc_ids": wb.get("linked_doc_ids") or [],  # [r232 #4] 정의서 연관 문서 자동 링크
            },
            "attachments": [],
            "assignees": [],
            "owner_user_id": user_id,
            "created_by": user_id,
            "sort_order": _now_ms(),
            "created_at": now,
            "updated_at": now,
        }
        try:
            store.client.table("wbs_nodes").insert(row).execute()
            created_wbs.append({"id": new_id, "title": wb.get("title")})
        except Exception as e:
            warnings.append(f"WBS 생성 실패({wb.get('title')}): {e}")

    for t in tasks:
        new_id = _task_id()
        row = {  # tasks 실제 컬럼만 (created_at/updated_by/history 컬럼 없음! updated_at=timestamptz)
            "id": new_id,
            "project_id": project_id,
            "title": t.get("title") or "태스크",
            "description": t.get("description") or "",
            "details": t.get("details") or "",
            "status": "pending",
            "zone": "shelf",
            "priority": t.get("priority", "p2"),
            # [r232] 항목별 스프린트 지정 + [r247] 신규 제안 스프린트 id 매핑 해석
            "sprint_id": sprint_id_map.get(t.get("sprint_id"), t.get("sprint_id")),
            "cat_id": t.get("cat_id"),         # [r232] 칸반 카테고리 (없으면 null)
            "due_date": None,
            "assignees": [],
            "linked_doc_ids": t.get("linked_doc_ids") or [],  # [r232] 정의서 연관 문서 자동 링크
            "created_by": user_id,
            "updated_at": now,
        }
        try:
            store.client.table("tasks").insert(row).execute()
            created_tasks.append({"id": new_id, "title": t.get("title")})
        except Exception as e:
            warnings.append(f"태스크 생성 실패({t.get('title')}): {e}")

    for iss in issues:
        new_id = _issue_id()
        row = {  # issues 실제 컬럼만
            "id": new_id,
            "project_id": project_id,
            "title": iss.get("title") or "이슈",
            "description": iss.get("description") or "",
            "status": iss.get("status", "open"),
            "priority": iss.get("priority", "p2"),
            "target": iss.get("target"),
            "due_date": None,
            "assignee_id": None,
            "created_by": user_id,
            "created_at": now,
            "updated_at": now,
        }
        try:
            store.client.table("issues").insert(row).execute()
            created_issues.append({"id": new_id, "title": iss.get("title")})
        except Exception as e:
            warnings.append(f"이슈 생성 실패({iss.get('title')}): {e}")

    return {
        "stages_created": created_stages,    # [r247]
        "sprints_created": created_sprints,   # [r247]
        "wbs_created": created_wbs,
        "tasks_created": created_tasks,
        "issues_created": created_issues,
        "warnings": warnings,
    }


def _guarded_insert(store, table: str, row: Dict[str, Any], optional_cols: set, warnings: List[str], label: str) -> bool:
    """[r253] write 가드 — 신규 컬럼 미생성 환경에서도 안전. 실패 시 optional 컬럼 제거 후 재시도."""
    try:
        store.client.table(table).insert(row).execute()
        return True
    except Exception:
        slim = {k: v for k, v in row.items() if k not in optional_cols}
        try:
            store.client.table(table).insert(slim).execute()
            warnings.append(f"{label}: 선택 컬럼 미생성 추정 → 일부 제외 저장({'/'.join(sorted(optional_cols))}). 마이그레이션 SQL 실행 권장")
            return True
        except Exception as e2:
            warnings.append(f"{label} 생성 실패: {e2}")
            return False


def apply_stream(
    *,
    session: Dict[str, Any],
    stages: List[Dict[str, Any]],
    sprints: List[Dict[str, Any]],
    tasks: List[Dict[str, Any]],
    wbs: List[Dict[str, Any]],
    user_id: str,
    project_id: Optional[str] = None,
    stream_id: Optional[str] = None,
    start_date: str = "2026-01-05",
    sprint_weeks: int = 2,
    default_cat_id: Optional[str] = None,
) -> Dict[str, Any]:
    """[r253] 도출 Stream(STAGE/Sprint/Task/WBS) 채택분을 실제 테이블에 생성.

    제안 id → 실제 id 리매핑(의존·세그먼트 참조 보존), 신규 컬럼은 write 가드.
    선택 채택: 넘어온 항목만 생성(프론트가 필터). 빈 배열이면 그 종류는 건너뜀.
    """
    require_author(user_id, "Stream 생성")
    store = get_store()
    now = _iso()
    sid_session = session.get("id")
    stream_id = stream_id or f"stream_{_now_ms()}"
    warnings: List[str] = []

    # ── 0. id/seq 매핑 사전 발급
    stage_map = {st.get("id"): _stage_id() for st in stages}          # 제안 stage id → 실제
    sprint_seq_map = {s.get("seq"): _sprint_id() for s in sprints}    # seq → 실제 sprint id
    task_map = {t.get("id"): _task_id() for t in tasks}              # 제안 task id → 실제
    wbs_map = {w.get("id"): _wbs_id() for w in wbs}                  # 제안 wbs id → 실제
    # seq → 소속 stage(실제 id)
    seq_to_stage = {}
    for st in stages:
        for sq in (st.get("sprint_seqs") or []):
            seq_to_stage[int(sq)] = stage_map.get(st.get("id"))
    # task 제안 id → 소속 sprint seq
    task_seq = {}
    for s in sprints:
        for tid in (s.get("task_ids") or []):
            task_seq[tid] = s.get("seq")

    created = {"stages": [], "sprints": [], "tasks": [], "wbs": []}

    # ── 1. STAGE → product_stages
    for st in stages:
        new_id = stage_map[st.get("id")]
        row = {
            "id": new_id, "project_id": project_id,
            "name": st.get("name") or "STAGE", "type": st.get("type") or "MVP",
            "goal": st.get("goal") or "", "lifecycle": st.get("lifecycle") or "build",
            "tags": st.get("tags") or [], "tag_meta": st.get("tag_meta") or {},
            "status": "active", "sort_order": _now_ms(),
            "stream_id": stream_id,
            "created_by": user_id, "created_at": now, "updated_at": now,
        }
        if _guarded_insert(store, "product_stages", row, {"stream_id"}, warnings, f"STAGE({st.get('name')})"):
            created["stages"].append({"id": new_id, "name": st.get("name")})

    # ── 2. Sprint → sprints (날짜·STAGE 연결·seq)
    for s in sprints:
        seq = s.get("seq")
        new_id = sprint_seq_map[seq]
        win = compute_sprint_window(seq, start_date, sprint_weeks)
        row = {
            "id": new_id, "project_id": project_id,
            "week_label": s.get("week_label") or f"Sprint {seq}",
            "goal": s.get("goal") or "", "status": "planning",
            "intrusion_count": 0, "intrusion_log": [], "carryover_from_previous": [],
            "milestone_criteria": [], "start_date": win["start"], "end_date": win["due"],
            "closed_at": None, "history": [], "participants": [],
            "stage_id": seq_to_stage.get(seq), "seq": seq, "stream_id": stream_id,
        }
        if _guarded_insert(store, "sprints", row, {"stage_id", "seq", "stream_id"}, warnings, f"Sprint({seq})"):
            created["sprints"].append({"id": new_id, "seq": seq})

    # ── 3. Task → tasks (공정태그·출처노드·의존·스프린트 연결)
    for t in tasks:
        new_id = task_map[t.get("id")]
        seq = task_seq.get(t.get("id"))
        deps_new = [task_map.get(d) for d in (t.get("depends_on") or []) if task_map.get(d)]
        row = {
            "id": new_id, "project_id": project_id,
            "title": t.get("title") or "작업", "description": t.get("desc") or t.get("description") or "",
            "details": "", "status": "pending", "zone": "shelf",
            "priority": "p2",
            "sprint_id": sprint_seq_map.get(seq), "cat_id": default_cat_id,
            "due_date": None, "assignees": [], "linked_doc_ids": [],
            "process_tag": t.get("process_tag") or "", "origin_node_id": t.get("node_id"),
            "depends_on": deps_new, "stream_id": stream_id,
            "created_by": user_id, "updated_at": now,
        }
        if _guarded_insert(store, "tasks", row, {"process_tag", "origin_node_id", "depends_on", "stream_id"}, warnings, f"Task({t.get('title')})"):
            created["tasks"].append({"id": new_id, "title": t.get("title")})

    # ── 4. WBS → wbs_nodes (세그먼트 sprint/task/deps 실제 id 리매핑)
    for w in wbs:
        new_id = wbs_map[w.get("id")]
        links = dict(w.get("links") or {})
        segs = []
        for seg in (links.get("_segments") or []):
            seg_id = seg.get("id")
            seq = None
            if isinstance(seg_id, str) and seg_id.startswith("seg_"):
                try:
                    seq = int(seg_id.split("_")[1])
                except Exception:
                    seq = None
            seg_sprint = [sprint_seq_map[seq]] if seq in sprint_seq_map else []
            seg_tasks = [task_map.get(x) for x in (seg.get("taskIds") or []) if task_map.get(x)]
            seg_deps = []
            for d in (seg.get("deps") or []):
                nn = wbs_map.get(d.get("n"), d.get("n"))
                seg_deps.append({"n": nn, "s": d.get("s")})
            segs.append({
                "id": seg_id, "start": seg.get("start"), "due": seg.get("due"),
                "sprintIds": seg_sprint, "taskIds": seg_tasks, "issueIds": [],
                "deps": seg_deps, "color": None,
            })
        new_links = {
            "_segments": segs,
            "taskIds": [task_map.get(x) for x in (links.get("taskIds") or []) if task_map.get(x)],
            "sprintIds": [sprint_seq_map[sq] for sq in (w.get("stage_seq_ref") or []) if sq in sprint_seq_map],
            "_estimated_days": links.get("_estimated_days"),
            "node_ids": links.get("node_ids") or [],
            "_stage_id": stage_map.get(links.get("_stage_id"), links.get("_stage_id")),
            "_source_session": sid_session,
        }
        parent = wbs_map.get(w.get("parent_id"), w.get("parent_id"))
        row = {
            "id": new_id, "project_id": project_id, "parent_id": parent,
            "title": w.get("title") or "WBS", "description": w.get("description") or "",
            "status": "todo", "progress": 0, "links": new_links,
            "attachments": [], "assignees": [], "owner_user_id": user_id,
            "created_by": user_id, "sort_order": _now_ms(),
            "stream_id": stream_id, "created_at": now, "updated_at": now,
        }
        if _guarded_insert(store, "wbs_nodes", row, {"stream_id"}, warnings, f"WBS({w.get('title')})"):
            created["wbs"].append({"id": new_id, "title": w.get("title")})

    return {
        "stream_id": stream_id,
        "stages_created": created["stages"], "sprints_created": created["sprints"],
        "tasks_created": created["tasks"], "wbs_created": created["wbs"],
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
