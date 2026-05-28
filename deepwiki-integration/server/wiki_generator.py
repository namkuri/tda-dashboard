"""[r113] 자동 위키 페이지 생성기 (Deep Wiki Phase A).

유니티(또는 일반) Git 레포를 분석해 LLM이 위키 페이지 N개를 생성한다.

흐름:
  1) Git clone (indexer가 사용한 경로 재사용)
  2) 파일 트리 스캔 → 주요 폴더(카테고리) 자동 추출
  3) 각 카테고리에 대해:
     - 그 폴더의 주요 코드 파일들을 모아 LLM에 전달
     - LLM이 마크다운 위키 페이지 작성 (개요·주요 클래스·의존성·코드 발췌)
  4) 전체 개요 페이지 1개 추가 (top-level + 카테고리 인덱스)
  5) deep_wiki_pages 테이블에 upsert

SSE 진행률 이벤트:
  {"event": "stage", "stage": "clone"|"scan"|"generate"|"save"}
  {"event": "progress", "current": i, "total": N, "category": "..."}
  {"event": "page_done", "slug": "...", "title": "..."}
  {"event": "done", "pages_created": M}
"""
import os
import re
import json
import shutil
import stat
from pathlib import Path
from typing import AsyncIterator, Dict, Any, List, Optional, Tuple
import git

from config import settings
from ollama_client import get_ollama
from supabase_store import get_store
from indexer import _safe_remove_repo, CODE_EXTENSIONS, SKIP_DIRS


# ─────────────────────────────────────────────
# 폴더/카테고리 분석
# ─────────────────────────────────────────────

# 카테고리 추론 시 우선시할 폴더 패턴 (유니티 + 일반)
# (regex, 추론 카테고리명, 슬러그) — 첫 매치 우선
_FOLDER_PATTERNS: List[Tuple[str, str, str]] = [
    # 유니티
    (r"Assets/Scripts/Managers?\b", "매니저 (Managers)", "managers"),
    (r"Assets/Scripts/Systems?\b", "시스템 (Systems)", "systems"),
    (r"Assets/Scripts/UI\b", "UI 시스템", "ui"),
    (r"Assets/Scripts/Data\b", "데이터 모델 (ScriptableObject)", "data-models"),
    (r"Assets/Scripts/Player\b", "플레이어 (Player)", "player"),
    (r"Assets/Scripts/Combat\b", "전투 시스템 (Combat)", "combat"),
    (r"Assets/Scripts/Enemy\b|Assets/Scripts/Enemies\b", "적 (Enemies)", "enemies"),
    (r"Assets/Scripts/Inventory\b", "인벤토리 (Inventory)", "inventory"),
    (r"Assets/Scripts/Quest\b|Assets/Scripts/Quests\b", "퀘스트 (Quests)", "quests"),
    (r"Assets/Scripts/Dialogue\b", "대화 시스템 (Dialogue)", "dialogue"),
    (r"Assets/Scripts/Save\b|Assets/Scripts/Persistence\b", "저장/직렬화 (Save)", "save"),
    (r"Assets/Scripts/Audio\b|Assets/Scripts/Sound\b", "오디오 (Audio)", "audio"),
    (r"Assets/Editor\b", "에디터 확장 (Editor)", "editor"),
    (r"Assets/Scripts\b", "기타 스크립트 (Scripts)", "scripts-misc"),
    # 일반 웹/앱
    (r"^src/components?\b", "컴포넌트 (Components)", "components"),
    (r"^src/pages?\b|^pages?\b", "페이지 (Pages)", "pages"),
    (r"^src/api\b|^api\b|^server\b", "API/서버", "api"),
    (r"^src/utils?\b|^lib\b|^utils?\b", "유틸 (Utils)", "utils"),
    (r"^src/services?\b", "서비스 (Services)", "services"),
    (r"^src/models?\b|^models?\b", "데이터 모델", "models"),
    (r"^src/store\b|^src/state\b|^store\b", "상태 관리 (Store)", "store"),
    (r"^src/hooks?\b|^hooks?\b", "훅 (Hooks)", "hooks"),
    (r"^src\b", "메인 소스 (src)", "src-main"),
    # 문서/스크립트
    (r"^docs?\b", "문서 (Docs)", "docs"),
    (r"^scripts?\b", "스크립트 (Scripts)", "scripts"),
    (r"^migrations?\b", "DB 마이그레이션", "migrations"),
    (r"^tests?\b|__tests__", "테스트 (Tests)", "tests"),
]


def _classify_file(rel_path: str) -> Optional[Tuple[str, str]]:
    """파일 경로를 카테고리(title, slug)로 분류. 매치 없으면 None."""
    norm = rel_path.replace("\\", "/")
    for pat, title, slug in _FOLDER_PATTERNS:
        if re.search(pat, norm, re.IGNORECASE):
            return title, slug
    return None


def _scan_repo(repo_path: Path, max_files_per_category: int = 12) -> Dict[str, Dict[str, Any]]:
    """파일 트리 스캔 → 카테고리별 파일 목록.

    Returns:
        { slug: {title, files: [{path, size, ext}], total_files} }
    """
    categories: Dict[str, Dict[str, Any]] = {}
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        if any(skip in path.parts for skip in SKIP_DIRS):
            continue
        if path.suffix.lower() not in CODE_EXTENSIONS:
            continue
        try:
            size = path.stat().st_size
            if size > settings.MAX_FILE_SIZE_KB * 1024:
                continue
        except Exception:
            continue
        rel = str(path.relative_to(repo_path)).replace("\\", "/")
        cls = _classify_file(rel)
        if not cls:
            # 기본 카테고리 = root
            cls = ("프로젝트 루트 (Root)", "root")
        title, slug = cls
        if slug not in categories:
            categories[slug] = {"title": title, "files": [], "total_files": 0}
        categories[slug]["total_files"] += 1
        # 카테고리당 max_files_per_category 까지 본문 후보
        if len(categories[slug]["files"]) < max_files_per_category:
            categories[slug]["files"].append({
                "path": rel,
                "abs": str(path),
                "size": size,
                "ext": path.suffix.lower(),
            })
    # 너무 작은 카테고리(파일 1~2개)는 root에 합쳐도 되지만 일단 유지
    return categories


def _read_file_truncated(abs_path: str, max_chars: int = 4000) -> str:
    """파일 본문 읽기 (너무 크면 잘라냄)."""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
            data = f.read(max_chars + 100)
        if len(data) > max_chars:
            return data[:max_chars] + "\n\n... (truncated)"
        return data
    except Exception as e:
        return f"(읽기 실패: {e})"


# ─────────────────────────────────────────────
# LLM 프롬프트
# ─────────────────────────────────────────────

_CATEGORY_PROMPT = """당신은 코드베이스 위키 작성자입니다. 아래 코드 파일들을 읽고
이 카테고리(시스템)에 대한 한국어 마크다운 위키 페이지를 작성하세요.

## 출력 형식 (반드시 이 구조)

# {{카테고리 제목}}

## 개요
이 시스템이 무엇을 하고 왜 존재하는지 2~4문장으로.

## 주요 클래스/함수/모듈
파일에서 핵심 식별자를 인라인 코드(`identifier`)로 인용 + 한 줄 역할 설명. 표 형식 권장:

| 이름 | 파일 | 역할 |
|------|------|------|
| `ClassName` | `path/to/file.cs` | ... |

## 내부 흐름 / 호출 관계
가능하면 함수 호출 흐름·이벤트·상태 전환을 화살표(`A → B`)로 표시. 복잡하면 Mermaid:
```mermaid
graph LR
  A --> B
```

## 핵심 코드 발췌
가장 중요한 부분 5~25줄을 코드 블록으로. 너무 길면 시그니처+한 줄 주석만.

## 관련 파일
- `path/to/file1.cs`
- `path/to/file2.cs`

## 메모
구현 시 주의할 점, 알려진 한계, TODO 등 (있을 때만).

## 규칙
- 한국어로 답변하되, 식별자·파일경로는 원문 유지
- 코드에 명시되지 않은 일반 상식 답변 금지 ("일반적으로 ~는...")
- 추측은 "~으로 보임" 표현으로 명시
- 코드에서 직접 인용한 사실만 적기

## 카테고리: {category_title}
## 슬러그: {slug}
## 파일 수: {total_files} (아래는 그 중 {shown_files}개 본문)

{files_block}
"""


def _build_files_block(files: List[Dict[str, Any]]) -> str:
    """파일 본문들을 LLM 프롬프트에 넣을 형식으로 조립."""
    blocks = []
    for f in files:
        text = _read_file_truncated(f["abs"], max_chars=3500)
        blocks.append(f"### `{f['path']}` ({f['size']} bytes)\n```\n{text}\n```")
    return "\n\n---\n\n".join(blocks)


async def _llm_generate_page(
    category_title: str,
    slug: str,
    files: List[Dict[str, Any]],
    total_files: int,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """LLM에게 카테고리 위키 페이지 작성 요청 → {title, content, summary}."""
    files_block = _build_files_block(files)
    prompt = _CATEGORY_PROMPT.format(
        category_title=category_title,
        slug=slug,
        total_files=total_files,
        shown_files=len(files),
        files_block=files_block,
    )
    ollama = get_ollama()
    # 단순 chat 호출 (도구 없음) — content가 곧 마크다운 페이지
    messages = [
        {"role": "system", "content": "당신은 친절하고 정확한 코드 위키 작성자입니다. 한국어로 답합니다."},
        {"role": "user", "content": prompt},
    ]
    # 비스트리밍, 더 큰 응답 허용
    content_chunks: List[str] = []
    async for d in ollama.chat_stream(messages=messages, model=model, temperature=0.3):
        content_chunks.append(d)
    content = "".join(content_chunks).strip()
    # 첫 줄에서 title 추출 (# 제목 라인) 또는 기본값
    title = category_title
    first_line = content.split("\n", 1)[0].strip()
    if first_line.startswith("#"):
        title = first_line.lstrip("# ").strip() or category_title
    # 요약: 개요 단락에서 첫 1~2문장 추출
    summary = _extract_summary(content)
    return {"title": title, "content": content, "summary": summary}


def _extract_summary(md: str) -> str:
    """마크다운에서 첫 일반 문단(개요)을 짧게 요약 추출."""
    # ## 개요 다음의 첫 단락
    m = re.search(r"##\s*개요\s*\n+([^\n#]+(?:\n[^\n#][^\n]*)*)", md)
    if m:
        text = m.group(1).strip()
    else:
        # 첫 비-제목·비-코드 단락
        parts = [p.strip() for p in md.split("\n\n")]
        text = next((p for p in parts if p and not p.startswith("#") and not p.startswith("```")), "")
    # 200자 컷
    text = re.sub(r"\s+", " ", text)
    return text[:200] + ("…" if len(text) > 200 else "")


# ─────────────────────────────────────────────
# 전체 파이프라인
# ─────────────────────────────────────────────

async def generate_wiki(
    git_url: str,
    project_id: str,
    branch: str = "main",
    clean_first: bool = True,
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Git 레포 → 1차 위키 페이지 N개 자동 생성."""
    if not project_id:
        yield {"event": "error", "message": "project_id 필수"}
        return
    if not git_url:
        yield {"event": "error", "message": "git_url 필수"}
        return

    store = get_store()
    ollama = get_ollama()
    if not await ollama.ping():
        yield {"event": "error", "message": "Ollama 연결 실패"}
        return

    # ─ 1) Git clone (indexer 패턴 재사용)
    clone_dir = Path(settings.GIT_CLONE_DIR)
    clone_dir.mkdir(parents=True, exist_ok=True)
    repo_name = git_url.rstrip("/").split("/")[-1].replace(".git", "")
    repo_path = clone_dir / repo_name
    yield {"event": "stage", "stage": "clone", "message": f"Git clone: {git_url}"}
    try:
        if repo_path.exists() and clean_first:
            _safe_remove_repo(repo_path)
        if not repo_path.exists():
            try:
                git.Repo.clone_from(git_url, repo_path, branch=branch, depth=1)
            except git.exc.GitCommandError as gce:
                detail = (gce.stderr or gce.stdout or str(gce)).strip()
                yield {"event": "error", "message": f"git clone 실패 ({gce.status}): {detail}"}
                return
        else:
            try:
                git.Repo(repo_path).remotes.origin.pull()
            except Exception:
                pass
    except Exception as e:
        yield {"event": "error", "message": f"clone 처리 실패: {type(e).__name__}: {e}"}
        return
    # 커밋 해시
    try:
        commit_hash = git.Repo(repo_path).head.commit.hexsha[:12]
    except Exception:
        commit_hash = None
    yield {"event": "clone_done", "commit": commit_hash}

    # ─ 2) 스캔
    yield {"event": "stage", "stage": "scan", "message": "파일 트리 분석 중..."}
    categories = _scan_repo(repo_path)
    # 너무 적은 카테고리는 root로 합치기 (파일 2개 미만)
    if "root" not in categories:
        categories["root"] = {"title": "프로젝트 루트 (Root)", "files": [], "total_files": 0}
    merged_slugs = []
    for slug in list(categories.keys()):
        if slug != "root" and categories[slug]["total_files"] < 2:
            for f in categories[slug]["files"]:
                if len(categories["root"]["files"]) < 12:
                    categories["root"]["files"].append(f)
                categories["root"]["total_files"] += 1
            merged_slugs.append(slug)
            del categories[slug]
    if not categories.get("root", {}).get("files"):
        categories.pop("root", None)
    cat_count = len(categories)
    yield {"event": "scan_done", "category_count": cat_count, "merged": merged_slugs}

    # ─ 3) 기존 페이지 삭제 (이 프로젝트의 자동 생성 페이지만)
    try:
        store.client.table("deep_wiki_pages").delete().eq("project_id", project_id).execute()
        yield {"event": "info", "message": f"기존 위키 페이지 정리됨"}
    except Exception as e:
        yield {"event": "warn", "message": f"기존 페이지 삭제 실패 (테이블 없음?): {e}"}

    # ─ 4) 카테고리별 LLM 생성 + 저장
    yield {"event": "stage", "stage": "generate", "message": f"LLM이 {cat_count}개 카테고리에 대해 위키 페이지 생성 중..."}
    created_pages: List[Dict[str, Any]] = []
    cat_items = sorted(categories.items(), key=lambda x: x[1]["total_files"], reverse=True)
    for idx, (slug, cat) in enumerate(cat_items):
        yield {
            "event": "progress",
            "current": idx + 1,
            "total": cat_count,
            "category": cat["title"],
            "slug": slug,
            "files_in_cat": cat["total_files"],
        }
        try:
            page = await _llm_generate_page(
                category_title=cat["title"],
                slug=slug,
                files=cat["files"],
                total_files=cat["total_files"],
                model=model,
            )
        except Exception as e:
            yield {"event": "warn", "message": f"LLM 실패 ({slug}): {type(e).__name__}: {e}"}
            continue
        # DB 저장
        page_id = f"dwp:{project_id}:{slug}"
        record = {
            "id": page_id,
            "project_id": project_id,
            "git_url": git_url,
            "git_commit": commit_hash,
            "slug": slug,
            "title": page["title"],
            "parent_slug": None,
            "sort_order": idx,
            "summary": page["summary"],
            "content": page["content"],
            "meta": {
                "category_title": cat["title"],
                "total_files": cat["total_files"],
                "files_used": [f["path"] for f in cat["files"]],
                "generated_by": settings.LLM_MODEL,
            },
        }
        try:
            store.client.table("deep_wiki_pages").upsert(record).execute()
            created_pages.append({"slug": slug, "title": page["title"]})
            yield {"event": "page_done", "slug": slug, "title": page["title"], "summary": page["summary"]}
        except Exception as e:
            yield {"event": "warn", "message": f"DB 저장 실패 ({slug}): {e}"}

    # ─ 5) 전체 개요 페이지 (overview) — 카테고리 인덱스
    yield {"event": "stage", "stage": "overview", "message": "개요 페이지 생성 중..."}
    try:
        overview_content = _build_overview_page(git_url, commit_hash, created_pages, categories)
        store.client.table("deep_wiki_pages").upsert({
            "id": f"dwp:{project_id}:_overview",
            "project_id": project_id,
            "git_url": git_url,
            "git_commit": commit_hash,
            "slug": "_overview",
            "title": "📘 프로젝트 개요",
            "parent_slug": None,
            "sort_order": -1,  # 항상 최상단
            "summary": f"{len(created_pages)}개 카테고리 자동 위키 ({commit_hash or '?'} 기준)",
            "content": overview_content,
            "meta": {"is_overview": True, "page_count": len(created_pages)},
        }).execute()
        yield {"event": "page_done", "slug": "_overview", "title": "📘 프로젝트 개요"}
    except Exception as e:
        yield {"event": "warn", "message": f"개요 페이지 실패: {e}"}

    yield {"event": "done", "pages_created": len(created_pages) + 1}


def _build_overview_page(git_url: str, commit: Optional[str], pages: List[Dict[str, Any]], categories: Dict[str, Dict[str, Any]]) -> str:
    """카테고리 인덱스 마크다운 페이지."""
    lines = [
        "# 📘 프로젝트 개요",
        "",
        f"**Git 레포:** [{git_url}]({git_url})",
        f"**커밋:** `{commit or '?'}`" if commit else "**커밋:** (미상)",
        f"**자동 생성 페이지:** {len(pages)}개",
        "",
        "이 위키는 LLM이 코드를 분석해 자동 작성한 1차 문서입니다. 카테고리별 페이지에서 시스템·클래스·흐름을 확인하세요.",
        "",
        "## 카테고리 목록",
        "",
        "| 슬러그 | 제목 | 파일 수 |",
        "|--------|------|---------|",
    ]
    by_slug = {p["slug"]: p for p in pages}
    for slug, cat in sorted(categories.items(), key=lambda x: x[1]["total_files"], reverse=True):
        title = by_slug.get(slug, {}).get("title") or cat["title"]
        lines.append(f"| [`{slug}`](#{slug}) | {title} | {cat['total_files']} |")
    lines.extend([
        "",
        "## 카테고리 페이지 링크",
        "",
    ])
    for p in pages:
        lines.append(f"- **[{p['title']}](?slug={p['slug']})** (`{p['slug']}`)")
    return "\n".join(lines)
