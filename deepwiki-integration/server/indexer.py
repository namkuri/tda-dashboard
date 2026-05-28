"""인덱서 — Git 코드, Supabase wiki_docs, 태스크, 스프린트 → 청크 → 임베딩 → pgvector."""
import os
import shutil
import stat
import traceback
from pathlib import Path
from typing import AsyncIterator, Optional, Dict, Any
import git  # GitPython

from config import settings
from chunker import chunk_text, chunk_code, count_tokens
from ollama_client import OllamaClient, get_ollama
from supabase_store import SupabaseStore, get_store


def _rm_readonly(func, path, exc_info):
    """Windows에서 .git의 읽기전용 파일 강제 삭제용 콜백.

    shutil.rmtree(..., onerror=_rm_readonly) 형식으로 사용.
    .git 객체 파일들이 read-only로 표시되어 일반 삭제가 안 되는 문제 해결.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass  # 정말 안 되면 무시 (clone_from이 실패하면 그때 명확한 에러)


def _safe_remove_repo(repo_path: Path) -> None:
    """레포 폴더 안전 삭제 — Windows 잠금 파일 처리 포함."""
    if not repo_path.exists():
        return
    # 1차: 일반 삭제
    shutil.rmtree(repo_path, onerror=_rm_readonly)
    # 2차: 그래도 남아있으면 chmod 후 재시도
    if repo_path.exists():
        for root, dirs, files in os.walk(repo_path):
            for d in dirs:
                try: os.chmod(os.path.join(root, d), stat.S_IWRITE)
                except Exception: pass
            for f in files:
                try: os.chmod(os.path.join(root, f), stat.S_IWRITE)
                except Exception: pass
        shutil.rmtree(repo_path, ignore_errors=True)


# [r95] 빈/플레이스홀더 콘텐츠 필터 — 짧은 한국어끼리 비교 시 false positive가 심해
# "여기에 내용을 작성하세요" 같은 기본 템플릿 텍스트가 0.9+ 유사도로 매칭되어
# 진짜 컨텐츠를 밀어내는 문제를 해결.
_EMPTY_TEMPLATE_PATTERNS = [
    # 위키 문서 기본 본문
    "여기에 내용을 작성하세요",
    "여기에 내용을 작성하세",  # 사용자가 뒤를 살짝 수정한 변종도 잡음
    # 태스크 기본 description / details
    "요약 설명을 입력하세요",
    "클릭하여 상세 내용을 마크다운으로 작성하세요",
    "상세 내용을 마크다운으로 작성하세요",
    # 영문 변종
    "Write your content here",
    "Click to write details",
    # 제목 자동 생성
    "새로운 태스크 (New Task)",
    "새로운 태스크",
    "(제목 없음)",
    "(No title)",
    "(Untitled)",
]
# 제목·플레이스홀더 빼고 남는 실질 글자 수 임계치 — 미만이면 인덱싱 스킵
_MIN_NONEMPTY_LEN = 30


def _is_empty_template_chunk(content: str, title: str = "") -> bool:
    """청크가 사실상 빈 템플릿/플레이스홀더만 포함하는지 검사.

    True → 인덱싱에서 제외 (벡터 검색에 노이즈 추가 방지).
    """
    if not content or not content.strip():
        return True
    t = content
    # 1. 최상단 `# 제목` 라인 제거
    lines = t.split("\n", 1)
    if lines and lines[0].lstrip().startswith("#"):
        t = lines[1] if len(lines) > 1 else ""
    # 2. 알려진 플레이스홀더 문구 제거
    for p in _EMPTY_TEMPLATE_PATTERNS:
        t = t.replace(p, "")
    # 3. 제목 자체가 플레이스홀더면 그것도 제거
    if title:
        for p in _EMPTY_TEMPLATE_PATTERNS:
            t = t.replace(p, "")
        t = t.replace(title, "")
    # 4. 공백·줄바꿈만 남으면 빈 청크
    stripped = "".join(t.split())
    return len(stripped) < _MIN_NONEMPTY_LEN


# 인덱싱할 코드 파일 확장자 (DeepWiki 참고 — 일반적 프로그래밍 언어)
CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".java", ".kt", ".cs", ".cpp", ".cc", ".c", ".h", ".hpp",
    ".go", ".rs", ".rb", ".php", ".swift", ".m", ".mm",
    ".html", ".css", ".scss", ".sass", ".less",
    ".sh", ".bash", ".zsh", ".ps1", ".bat",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".env.example",
    ".md", ".rst", ".txt",
    ".sql", ".graphql", ".proto",
    ".lua", ".dart", ".scala", ".clj", ".ex", ".exs",
}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
             "dist", "build", ".next", "target", ".idea", ".vscode",
             "coverage", ".pytest_cache", ".mypy_cache"}


async def index_git_repo(
    git_url: str,
    project_id: Optional[str] = None,
    branch: str = "main",
    clean_first: bool = True,
) -> AsyncIterator[Dict[str, Any]]:
    """Git 레포 clone → 파일 트리 순회 → 청크 → 임베딩 → 저장.

    yields 진행률 이벤트:
        {"event": "start", "total_files": N}
        {"event": "progress", "current": i, "total": N, "file": "path"}
        {"event": "done", "chunks_inserted": M}
        {"event": "error", "message": "..."}
    """
    ollama = get_ollama()
    store = get_store()

    # 1. clone
    clone_dir = Path(settings.GIT_CLONE_DIR)
    clone_dir.mkdir(parents=True, exist_ok=True)
    repo_name = git_url.rstrip("/").split("/")[-1].replace(".git", "")
    repo_path = clone_dir / repo_name

    try:
        if repo_path.exists() and clean_first:
            yield {"event": "info", "message": f"기존 폴더 정리 중: {repo_path.name}"}
            _safe_remove_repo(repo_path)
            if repo_path.exists():
                yield {"event": "error", "message": f"기존 폴더 삭제 실패 — 수동으로 삭제 필요: {repo_path}"}
                return
        if not repo_path.exists():
            yield {"event": "clone_start", "repo": repo_name, "url": git_url}
            try:
                git.Repo.clone_from(git_url, repo_path, branch=branch, depth=1)
            except git.exc.GitCommandError as gce:
                # [r93] GitPython 에러는 stderr를 포함해 자세한 정보 노출
                stderr = (gce.stderr or "").strip()
                stdout = (gce.stdout or "").strip()
                detail = stderr or stdout or str(gce)
                yield {"event": "error", "message": f"git clone 실패 ({gce.status}): {detail}"}
                return
            yield {"event": "clone_done"}
        else:
            # 기존 clone — pull
            try:
                repo = git.Repo(repo_path)
                repo.remotes.origin.pull()
                yield {"event": "pull_done"}
            except Exception as pe:
                yield {"event": "error", "message": f"git pull 실패: {pe} — clean_first=True로 재시도 권장"}
                return
    except Exception as e:
        # 알 수 없는 예외 — traceback 포함
        tb = traceback.format_exc().splitlines()[-3:]
        yield {"event": "error", "message": f"clone 처리 실패: {type(e).__name__}: {e}", "trace": tb}
        return

    # 2. 기존 청크 삭제
    if clean_first:
        deleted = store.delete_by_source("code")  # 전체 코드 청크 삭제
        yield {"event": "cleaned", "deleted": deleted}

    # 3. 파일 순회
    all_files = []
    skipped_by_size = []  # [r98] 크기 초과로 스킵된 파일 명세 (사용자가 즉시 확인 가능)
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        # 디렉토리 스킵
        if any(skip in path.parts for skip in SKIP_DIRS):
            continue
        # 확장자 화이트리스트
        if path.suffix.lower() not in CODE_EXTENSIONS:
            continue
        # 크기 제한
        try:
            sz = path.stat().st_size
            if sz > settings.MAX_FILE_SIZE_KB * 1024:
                # [r98] 스킵된 파일 명세 기록 — 사용자가 어떤 파일이 누락됐는지 알 수 있게
                rel = str(path.relative_to(repo_path)).replace("\\", "/")
                skipped_by_size.append({"file": rel, "size_kb": round(sz / 1024, 1)})
                continue
        except Exception:
            continue
        all_files.append(path)

    # [r98] 크기 초과 스킵 파일을 warn 이벤트로 명시 알림
    if skipped_by_size:
        # 상위 5개만 메시지에 포함
        sample = ", ".join([f'{s["file"]}({s["size_kb"]}KB)' for s in skipped_by_size[:5]])
        more = f" 외 {len(skipped_by_size)-5}개" if len(skipped_by_size) > 5 else ""
        yield {
            "event": "warn",
            "message": f"⚠ 크기 초과({settings.MAX_FILE_SIZE_KB}KB)로 스킵된 파일 {len(skipped_by_size)}개: {sample}{more}. "
                       f"필요시 .env에 MAX_FILE_SIZE_KB를 더 크게 설정 후 재인덱싱.",
            "skipped_files": skipped_by_size,
        }

    total = len(all_files)
    yield {"event": "start", "total_files": total}

    chunks_inserted = 0
    batch = []
    BATCH_SIZE = 32  # 한 번에 upsert할 청크 수

    for idx, file_path in enumerate(all_files):
        rel_path = str(file_path.relative_to(repo_path)).replace("\\", "/")
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue

        # 코드 청킹
        chunks = chunk_code(text, file_path=rel_path)
        for ci, chunk in enumerate(chunks):
            try:
                embedding = await ollama.embed(chunk)
            except Exception as e:
                yield {"event": "warn", "message": f"임베딩 실패 {rel_path}: {e}"}
                continue
            batch.append({
                "project_id": project_id,
                "source_type": "code",
                "source_id": rel_path,
                "source_path": rel_path,
                "source_title": rel_path,
                "chunk_idx": ci,
                "content": chunk,
                "embedding": embedding,
                "token_count": count_tokens(chunk),
            })
            if len(batch) >= BATCH_SIZE:
                try:
                    store.upsert_chunks(batch)
                    chunks_inserted += len(batch)
                except Exception as e:
                    yield {"event": "warn", "message": f"upsert 실패: {e}"}
                batch = []
        yield {"event": "progress", "current": idx + 1, "total": total, "file": rel_path}

    # 마지막 배치
    if batch:
        try:
            store.upsert_chunks(batch)
            chunks_inserted += len(batch)
        except Exception as e:
            yield {"event": "warn", "message": f"마지막 upsert 실패: {e}"}

    yield {"event": "done", "chunks_inserted": chunks_inserted}


async def index_wiki_docs(project_id: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    """Supabase wiki_docs 전체 → 청크 → 임베딩."""
    ollama = get_ollama()
    store = get_store()

    # [r103] select("*")로 안전 — 스키마에 없는 컬럼은 무시
    q = store.client.table("wiki_docs").select("*")
    if project_id:
        q = q.eq("project_id", project_id)
    res = q.execute()
    docs = res.data or []

    # 폴더 제외
    docs = [d for d in docs if not (d.get("meta") or {}).get("isFolder")]

    yield {"event": "start", "total_docs": len(docs)}

    # 기존 wiki 청크 삭제
    if project_id:
        deleted = store.delete_by_project(project_id, source_type="wiki")
    else:
        deleted = store.delete_by_source("wiki")
    yield {"event": "cleaned", "deleted": deleted}

    chunks_inserted = 0
    skipped_empty = 0
    batch = []
    BATCH_SIZE = 32

    for idx, doc in enumerate(docs):
        title = doc.get("title", "(제목 없음)")
        content = doc.get("content") or ""
        kind = doc.get("kind", "wiki")
        if not content.strip():
            continue
        # [r95] 빈 템플릿 (예: "여기에 내용을 작성하세요") 스킵 — 노이즈 매칭 방지
        if _is_empty_template_chunk(content, title):
            skipped_empty += 1
            continue
        chunks = chunk_text(content)
        for ci, chunk in enumerate(chunks):
            try:
                embedding = await ollama.embed(chunk)
            except Exception as e:
                yield {"event": "warn", "message": f"임베딩 실패 {title}: {e}"}
                continue
            batch.append({
                "project_id": doc.get("project_id"),
                "source_type": "wiki",
                "source_id": doc["id"],
                "source_path": kind + ":" + title,
                "source_title": title,
                "chunk_idx": ci,
                "content": chunk,
                "embedding": embedding,
                "token_count": count_tokens(chunk),
            })
            if len(batch) >= BATCH_SIZE:
                try:
                    store.upsert_chunks(batch)
                    chunks_inserted += len(batch)
                except Exception as e:
                    yield {"event": "warn", "message": f"upsert 실패: {e}"}
                batch = []
        yield {"event": "progress", "current": idx + 1, "total": len(docs), "file": title}

    if batch:
        store.upsert_chunks(batch)
        chunks_inserted += len(batch)

    yield {"event": "done", "chunks_inserted": chunks_inserted, "skipped_empty": skipped_empty}


async def index_tasks(project_id: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    """Supabase tasks → 청크 → 임베딩.

    [r102→r103] 라이브 메타(상태·존·담당자 등) content에 포함.
    r103: select("*")로 모든 컬럼 안전 조회 — 스키마에 없는 컬럼은 무시.
    실제 컬럼: dev_name(legacy), assignees(jsonb 배열), due_date,
              is_starred, carryover_count, zone, sprint_id, etc.
    """
    ollama = get_ollama()
    store = get_store()

    # [r103] select("*")로 안전하게 — 어떤 컬럼이 있든 에러 안 남
    q = store.client.table("tasks").select("*")
    if project_id:
        q = q.eq("project_id", project_id)
    res = q.execute()
    tasks = res.data or []

    yield {"event": "start", "total_tasks": len(tasks)}

    # 기존 task 청크 삭제
    if project_id:
        store.delete_by_project(project_id, source_type="task")
    else:
        store.delete_by_source("task")

    chunks_inserted = 0
    skipped_empty = 0
    batch = []
    BATCH_SIZE = 32

    for idx, t in enumerate(tasks):
        title = t.get("title", "")
        body = "\n\n".join(filter(None, [
            t.get("description", ""),
            t.get("details", ""),
        ]))
        if not (title or body).strip():
            continue
        # [r102] 상태 메타를 본문 머리에 추가 — LLM이 "진행중/완료/마감임박" 판별 가능
        meta_lines = ["## 메타"]
        status = t.get("status") or "?"
        zone = t.get("zone") or "?"
        prio = t.get("priority") or "?"
        # 상태를 영문+한국어 양쪽으로 — LIKE 검색 보강
        status_kr = {"pending": "대기중", "progress": "진행중", "completed": "완료"}.get(status, status)
        zone_kr = {"now": "Now (지금)", "shelf": "Shelf (선반)", "buried": "Buried (묻힘)"}.get(zone, zone)
        meta_lines.append(f"- 상태: {status} ({status_kr})")
        meta_lines.append(f"- Zone: {zone_kr}")
        meta_lines.append(f"- 우선순위: {prio}")
        if t.get("sprint_id"):
            meta_lines.append(f"- 스프린트 ID: {t['sprint_id']}")
        if t.get("due_date"):
            meta_lines.append(f"- 마감일 (due_date): {t['due_date']}")
        # [r103] 실제 컬럼명 — assignees(jsonb 배열), dev_name(legacy 담당자 이름)
        assignees = t.get("assignees") or []
        if isinstance(assignees, list) and assignees:
            meta_lines.append(f"- 담당자 (assignees): {len(assignees)}명 ({', '.join(str(a) for a in assignees[:3])})")
        if t.get("dev_name"):
            meta_lines.append(f"- 개발자 이름 (dev_name): {t['dev_name']}")
        if t.get("cat_id"):
            meta_lines.append(f"- 카테고리 ID (cat_id): {t['cat_id']}")
        if t.get("cat_badge"):
            meta_lines.append(f"- 카테고리 뱃지: {t['cat_badge']}")
        if t.get("is_starred"):
            meta_lines.append("- ⭐ 별표 (starred)")
        if t.get("carryover_count"):
            meta_lines.append(f"- 이월 횟수 (carryoverCount): {t['carryover_count']}")
        if t.get("bury_reason"):
            meta_lines.append(f"- Bury 사유: {t['bury_reason']}")
        if t.get("done_at"):
            meta_lines.append(f"- 완료 시각: {t['done_at']}")
        meta_block = "\n".join(meta_lines)
        full_text = f"# {title}\n\n{meta_block}\n\n## 본문\n{body}"
        # [r95] 기본 템플릿 태스크 ("새로운 태스크 (New Task)" + "요약 설명을 입력하세요") 스킵
        if _is_empty_template_chunk(full_text, title):
            skipped_empty += 1
            continue
        chunks = chunk_text(full_text)
        for ci, chunk in enumerate(chunks):
            try:
                embedding = await ollama.embed(chunk)
            except Exception:
                continue
            batch.append({
                "project_id": t.get("project_id"),
                "source_type": "task",
                "source_id": t["id"],
                "source_path": f"task:{title[:40]}",
                "source_title": title or "(제목 없음)",
                "chunk_idx": ci,
                "content": chunk,
                "embedding": embedding,
                "token_count": count_tokens(chunk),
            })
            if len(batch) >= BATCH_SIZE:
                store.upsert_chunks(batch)
                chunks_inserted += len(batch)
                batch = []
        if (idx + 1) % 10 == 0:
            yield {"event": "progress", "current": idx + 1, "total": len(tasks)}

    if batch:
        store.upsert_chunks(batch)
        chunks_inserted += len(batch)

    yield {"event": "done", "chunks_inserted": chunks_inserted, "skipped_empty": skipped_empty}


async def index_sprints(project_id: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    """Supabase sprints (goal/checklists + status + dates + participants) → 임베딩.

    [r102→r103] 라이브 메타 포함. r103: select("*")로 안전 조회.
    실제 컬럼: status, intrusion_count, intrusion_log, carryover_from_previous,
              milestone_criteria, start_date, end_date, closed_at, history,
              participants, section_order, attachments, checklists.
    """
    ollama = get_ollama()
    store = get_store()

    # [r103] select("*")로 안전 — 스키마에 없는 컬럼은 자동 무시
    q = store.client.table("sprints").select("*")
    if project_id:
        q = q.eq("project_id", project_id)
    res = q.execute()
    sprints = res.data or []

    yield {"event": "start", "total_sprints": len(sprints)}

    if project_id:
        store.delete_by_project(project_id, source_type="sprint")
    else:
        store.delete_by_source("sprint")

    chunks_inserted = 0
    skipped_empty = 0
    batch = []

    for idx, sp in enumerate(sprints):
        wlabel = sp.get("week_label", "스프린트")
        goal = sp.get("goal", "")
        checklists = sp.get("checklists") or []
        checklist_text = ""
        if isinstance(checklists, list):
            for cl in checklists:
                if isinstance(cl, dict) and cl.get("text"):
                    checklist_text += "- " + cl["text"] + "\n"

        # [r102] 상태·날짜·끼어들기 메타를 머리에 추가 — 영문+한국어 둘 다로 LIKE 검색 보강
        status = sp.get("status") or "planned"
        status_kr = {"active": "진행중 (active)", "closed": "종료됨 (closed)", "planned": "예정 (planned)"}.get(status, status)
        meta_lines = ["## 스프린트 메타", f"- ID: {sp.get('id')}", f"- 상태: {status_kr}"]
        if sp.get("start_date"):
            meta_lines.append(f"- 시작일 (startDate): {sp['start_date']}")
        if sp.get("end_date"):
            meta_lines.append(f"- 종료일 (endDate): {sp['end_date']}")
        if sp.get("intrusion_count") is not None:
            meta_lines.append(f"- 끼어들기 카운트 (intrusionCount): {sp['intrusion_count']}")
        intrusion_log = sp.get("intrusion_log") or []
        if isinstance(intrusion_log, list) and intrusion_log:
            meta_lines.append(f"- 끼어들기 기록 {len(intrusion_log)}건")
        carryover = sp.get("carryover_from_previous") or []
        if isinstance(carryover, list) and carryover:
            meta_lines.append(f"- 이전 스프린트 이월 카드 {len(carryover)}개")
        participants = sp.get("participants") or []
        if isinstance(participants, list) and participants:
            meta_lines.append(f"- 참여자 {len(participants)}명")
        if sp.get("closed_at"):
            meta_lines.append(f"- 종료 시각: {sp['closed_at']}")
        meta_block = "\n".join(meta_lines)
        full = f"# 스프린트 {wlabel}\n\n{meta_block}\n\n## 목표 (goal)\n{goal}\n\n## 체크리스트 (checklists)\n{checklist_text}"
        if not full.strip():
            continue
        # [r95] 목표·체크리스트 모두 비어있으면 스킵 (단, 메타 자체로도 정보 있음 — 임계치 완화)
        if _is_empty_template_chunk(full, wlabel) and not status:
            skipped_empty += 1
            continue
        chunks = chunk_text(full)
        for ci, chunk in enumerate(chunks):
            try:
                embedding = await ollama.embed(chunk)
            except Exception:
                continue
            batch.append({
                "project_id": sp.get("project_id"),
                "source_type": "sprint",
                "source_id": sp["id"],
                "source_path": f"sprint:{wlabel}",
                "source_title": wlabel,
                "chunk_idx": ci,
                "content": chunk,
                "embedding": embedding,
                "token_count": count_tokens(chunk),
            })
        if batch and len(batch) >= 16:
            store.upsert_chunks(batch)
            chunks_inserted += len(batch)
            batch = []

    if batch:
        store.upsert_chunks(batch)
        chunks_inserted += len(batch)

    yield {"event": "done", "chunks_inserted": chunks_inserted, "skipped_empty": skipped_empty}
