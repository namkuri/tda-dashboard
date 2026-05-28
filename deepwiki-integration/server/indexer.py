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
            if path.stat().st_size > settings.MAX_FILE_SIZE_KB * 1024:
                continue
        except Exception:
            continue
        all_files.append(path)

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

    # wiki_docs 가져오기
    q = store.client.table("wiki_docs").select("id,title,content,kind,project_id,meta")
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
    batch = []
    BATCH_SIZE = 32

    for idx, doc in enumerate(docs):
        title = doc.get("title", "(제목 없음)")
        content = doc.get("content") or ""
        kind = doc.get("kind", "wiki")
        if not content.strip():
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

    yield {"event": "done", "chunks_inserted": chunks_inserted}


async def index_tasks(project_id: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    """Supabase tasks → 청크 → 임베딩."""
    ollama = get_ollama()
    store = get_store()

    q = store.client.table("tasks").select("id,title,description,details,project_id,cat_id")
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
        full_text = f"# {title}\n\n{body}"
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

    yield {"event": "done", "chunks_inserted": chunks_inserted}


async def index_sprints(project_id: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    """Supabase sprints (goal/checklists) → 임베딩."""
    ollama = get_ollama()
    store = get_store()

    q = store.client.table("sprints").select("id,week_label,goal,checklists,project_id")
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

        full = f"# 스프린트 {wlabel}\n\n## 목표\n{goal}\n\n## 체크리스트\n{checklist_text}"
        if not full.strip():
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

    yield {"event": "done", "chunks_inserted": chunks_inserted}
