"""[r208] 신규 엔티티 인덱서 — issue/event/asset/review/bug/wbs

indexer.py와 같은 패턴: Supabase 테이블 → 항목별 텍스트 본문 합성 → 청크 → 임베딩 →
doc_chunks(source_type=<kind>) 저장. since 증분 + project_id 필터 지원.
indexer.py에서 import하여 사용.
"""
import json as _json
from typing import AsyncIterator, Dict, Any, Optional

from ollama_client import get_ollama
from supabase_store import get_store
from chunker import chunk_text, count_tokens


async def _index_entity(
    *,
    table: str,
    source_type: str,
    project_id: Optional[str],
    since: Optional[str],
    title_fn,
    body_fn,
    use_project_filter: bool = True,
) -> AsyncIterator[Dict[str, Any]]:
    ollama = get_ollama()
    store = get_store()
    try:
        q = store.client.table(table).select("*")
        if use_project_filter and project_id:
            q = q.eq("project_id", project_id)
        if since:
            try:
                q = q.gte("updated_at", since)
            except Exception:
                pass
        res = q.execute()
        rows = res.data or []
    except Exception as e:
        yield {"event": "warn", "message": f"{table} 테이블 조회 실패(미생성 가능): {e}"}
        yield {"event": "done", "chunks_inserted": 0, "skipped_empty": 0}
        return

    yield {"event": "start", "total_docs": len(rows), "mode": "incremental" if since else "full", "source_type": source_type}

    if since and rows:
        for row in rows:
            sid = row.get("id")
            if not sid:
                continue
            try:
                store.client.table("doc_chunks").delete().eq("source_type", source_type).eq("source_id", sid).execute()
            except Exception:
                pass
        yield {"event": "cleaned", "deleted": len(rows), "mode": "by_source_id"}
    elif since:
        yield {"event": "cleaned", "deleted": 0, "mode": "no_changes"}
    else:
        deleted = 0
        try:
            if use_project_filter and project_id:
                deleted = store.delete_by_project(project_id, source_type=source_type)
            else:
                deleted = store.delete_by_source(source_type)
        except Exception:
            pass
        yield {"event": "cleaned", "deleted": deleted, "mode": "full_wipe"}

    chunks_inserted = 0
    skipped_empty = 0
    batch = []
    BATCH = 32
    for idx, row in enumerate(rows):
        title = title_fn(row) or "(제목 없음)"
        body = body_fn(row) or ""
        if not body.strip():
            skipped_empty += 1
            continue
        path = f"{source_type}:{title}"
        chunks = chunk_text(body)
        for ci, ch in enumerate(chunks):
            try:
                emb = await ollama.embed(ch)
            except Exception as e:
                yield {"event": "warn", "message": f"임베딩 실패 {title}: {e}"}
                continue
            batch.append({
                "project_id": row.get("project_id"),
                "source_type": source_type,
                "source_id": row.get("id"),
                "source_path": path,
                "source_title": title,
                "chunk_idx": ci,
                "content": ch,
                "embedding": emb,
                "token_count": count_tokens(ch),
            })
            if len(batch) >= BATCH:
                try:
                    store.upsert_chunks(batch)
                    chunks_inserted += len(batch)
                except Exception as e:
                    yield {"event": "warn", "message": f"upsert 실패: {e}"}
                batch = []
        if (idx + 1) % 5 == 0 or idx + 1 == len(rows):
            yield {"event": "progress", "current": idx + 1, "total": len(rows), "file": title}
    if batch:
        try:
            store.upsert_chunks(batch)
            chunks_inserted += len(batch)
        except Exception as e:
            yield {"event": "warn", "message": f"upsert 실패: {e}"}
    yield {"event": "done", "chunks_inserted": chunks_inserted, "skipped_empty": skipped_empty}


async def index_issues(project_id: Optional[str] = None, since: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    def t(r): return r.get("title") or "(제목 없음)"
    def b(r):
        return "\n".join([
            f"# 🐛 이슈: {r.get('title','')}",
            f"우선순위: {r.get('priority','-')} | 상태: {r.get('status','-')}",
            f"담당자: {r.get('assignee_id') or '미지정'}",
            f"마감: {r.get('due_date') or '-'}",
            f"대상: {r.get('target') or '-'}",
            "",
            r.get("description") or "",
        ])
    async for ev in _index_entity(table="issues", source_type="issue",
                                  project_id=project_id, since=since,
                                  title_fn=t, body_fn=b):
        yield ev


async def index_calendar_events(project_id: Optional[str] = None, since: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    def t(r): return r.get("title") or "(제목 없음)"
    def b(r):
        return "\n".join([
            f"# 📅 일정: {r.get('title','')}",
            f"시작: {r.get('start_at') or '-'} → 종료: {r.get('end_at') or '-'}",
            f"공개: {'예' if r.get('is_public') else '아니오'} | 담당: {r.get('owner_user_id') or '-'}",
            "",
            r.get("description") or "",
        ])
    async for ev in _index_entity(table="calendar_events", source_type="event",
                                  project_id=project_id, since=since,
                                  title_fn=t, body_fn=b):
        yield ev


async def index_assets(project_id: Optional[str] = None, since: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    def t(r): return r.get("name") or "(이름 없음)"
    def b(r):
        return "\n".join([
            f"# 🎮 에셋: {r.get('name','')}",
            f"종류: {r.get('kind') or '-'} | 상태: {r.get('status') or '-'}",
            f"담당: {r.get('assignee_id') or '-'} | 폴더: {r.get('folder_id') or '-'}",
            f"파일: {r.get('file_link') or '-'}",
            "",
            r.get("notes") or "",
        ])
    async for ev in _index_entity(table="assets", source_type="asset",
                                  project_id=project_id, since=since,
                                  title_fn=t, body_fn=b):
        yield ev


async def index_reviews(project_id: Optional[str] = None, since: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    def t(r): return r.get("title") or "(제목 없음)"
    def b(r):
        log = r.get("log") or []
        log_txt = ""
        if isinstance(log, list):
            try:
                log_txt = _json.dumps(log[-10:], ensure_ascii=False)
            except Exception:
                log_txt = str(log[-10:])
        return "\n".join([
            f"# 🗳 리뷰: {r.get('title','')}",
            f"타입: {r.get('type') or '-'} | 상태: {r.get('status') or '-'}",
            f"기안: {r.get('proposer_name') or '-'} | 대상 문서: {r.get('target_doc_id') or '-'} | 대상 카드: {r.get('target_task_id') or '-'}",
            "",
            "최근 로그:",
            log_txt,
        ])
    async for ev in _index_entity(table="review_requests", source_type="review",
                                  project_id=project_id, since=since,
                                  title_fn=t, body_fn=b):
        yield ev


async def index_bug_reports(project_id: Optional[str] = None, since: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    def t(r): return r.get("title") or "(제목 없음)"
    def b(r):
        return "\n".join([
            f"# 🐞 버그: {r.get('title','')}",
            f"심각도: {r.get('severity') or '-'} | 상태: {r.get('status') or '-'}",
            f"발생 화면: {r.get('view') or '-'} | 환경: {r.get('env') or '-'}",
            "",
            r.get("description") or "",
        ])
    # bug_reports는 project_id 컬럼이 없을 수 있어 use_project_filter=False
    async for ev in _index_entity(table="bug_reports", source_type="bug",
                                  project_id=project_id, since=since,
                                  title_fn=t, body_fn=b, use_project_filter=False):
        yield ev


async def index_wbs_nodes(project_id: Optional[str] = None, since: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    """작업 구조화(WBS) 노드 → 청크. links._segments(타임라인 다중 일정) 본문 포함."""
    def t(r): return r.get("title") or "(제목 없음)"
    def b(r):
        links = r.get("links") or {}
        segs = links.get("_segments") if isinstance(links, dict) else None
        seg_lines = []
        if isinstance(segs, list):
            for i, s in enumerate(segs, 1):
                if not isinstance(s, dict):
                    continue
                seg_lines.append(
                    f"  - 일정 #{i}: {s.get('start') or '?'} ~ {s.get('due') or '?'}"
                    f" | 스프린트={(s.get('sprintIds') or [])}"
                    f" | 태스크={(s.get('taskIds') or [])}"
                    f" | 이슈={(s.get('issueIds') or [])}"
                )
        link_lines = []
        if isinstance(links, dict):
            for k in ("sprintIds", "categoryIds", "taskIds", "assetIds", "assetFolderIds", "linkIds"):
                v = links.get(k)
                if v:
                    link_lines.append(f"  - {k}: {v}")
        parts = [
            f"# 🧩 작업노드: {r.get('title','')}",
            f"상태: {r.get('status') or '-'} | 진척: {r.get('progress') or 0}%",
            f"기간: {(links.get('_start') if isinstance(links, dict) else None) or '-'} ~ {(links.get('_due') if isinstance(links, dict) else None) or '-'}",
            f"담당자: {(r.get('assignees') or [])} | 부모: {r.get('parent_id') or '루트'}",
        ]
        if seg_lines:
            parts.append("\n## 일정 세그먼트(다중 진행바)")
            parts.extend(seg_lines)
        if link_lines:
            parts.append("\n## 연결")
            parts.extend(link_lines)
        parts.append("")
        parts.append(r.get("description") or "")
        return "\n".join(parts)
    async for ev in _index_entity(table="wbs_nodes", source_type="wbs",
                                  project_id=project_id, since=since,
                                  title_fn=t, body_fn=b):
        yield ev
