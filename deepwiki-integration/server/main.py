"""FastAPI 엔트리포인트 — /health, /chat, /index/*."""
import asyncio
import json
import time
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from config import settings
from ollama_client import get_ollama
from supabase_store import get_store
from rag import chat_stream as legacy_chat_stream  # [r108] 폴백용
from agent import run as agent_run  # [r108] Tool Use 에이전트
from retriever import retrieve, SIMILARITY_THRESHOLD
from indexer import index_git_repo, index_wiki_docs, index_tasks, index_sprints, _is_empty_template_chunk


app = FastAPI(title="TDA Deep Wiki", version="1.0.0")
START_TIME = time.time()

# CORS — GitHub Pages 도메인 등 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=r"https://.*\.github\.io|http://localhost:\d+",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# Models (Pydantic)
# ─────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str  # 'user' | 'assistant' | 'system'
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    project_id: Optional[str] = None
    model: Optional[str] = None
    include_tasks: bool = True
    stream: bool = True


class IndexCodeRequest(BaseModel):
    git_url: str
    project_id: Optional[str] = None
    branch: str = "main"
    generate_wiki: bool = False  # 향후: 파일별 LLM 요약을 wiki_docs로 저장


class IndexWikiRequest(BaseModel):
    project_id: Optional[str] = None


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────

@app.get("/health")
async def health(verbose: bool = False):
    """서버 + 의존 서비스 상태.

    [r97] ?verbose=true 시 source_id별 청크 수 상위 15개 진단 정보 포함.
    public/index.html 같은 파일이 인덱싱 됐는지 즉시 확인 가능.
    """
    ollama = get_ollama()
    store = get_store()
    ollama_ok = await ollama.ping()
    models = await ollama.list_models() if ollama_ok else []
    supabase_ok = store.ping()
    stats = store.stats() if supabase_ok else {}
    out = {
        "status": "ok" if (ollama_ok and supabase_ok) else "degraded",
        "model": settings.LLM_MODEL,
        "embed_model": settings.EMBED_MODEL,
        "ollama": "connected" if ollama_ok else "disconnected",
        "ollama_models": models,
        "supabase": "connected" if supabase_ok else "disconnected",
        "chunks": stats,
        "uptime_sec": int(time.time() - START_TIME),
    }
    if verbose and supabase_ok:
        out["top_sources"] = store.top_sources(limit=15)
    return out


@app.post("/chat")
async def chat(req: ChatRequest):
    """RAG 챗 — SSE 스트리밍 또는 단건 JSON."""
    if not req.messages:
        raise HTTPException(400, "messages가 비어있습니다")

    # [r108] 기본은 Agentic Tool-Use 에이전트. 'legacy' 모델 명시 시 옛 RAG.
    use_agent = (req.model or "").lower() != "legacy"
    chat_fn = agent_run if use_agent else legacy_chat_stream

    if req.stream:
        async def gen():
            try:
                async for event in chat_fn(
                    messages=[m.model_dump() for m in req.messages],
                    project_id=req.project_id,
                    model=req.model if not use_agent or req.model != "legacy" else None,
                    include_tasks=req.include_tasks,
                ):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                import traceback
                tb = traceback.format_exc().splitlines()[-3:]
                yield f"data: {json.dumps({'delta': f'❌ 오류: {e}', 'trace': tb}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")
    else:
        # 비스트리밍: 전체 응답을 모아서 한 번에
        content = ""
        sources = []
        meta = None
        async for event in chat_fn(
            messages=[m.model_dump() for m in req.messages],
            project_id=req.project_id,
            model=req.model if not use_agent or req.model != "legacy" else None,
            include_tasks=req.include_tasks,
        ):
            if "delta" in event:
                content += event["delta"]
            if "sources" in event:
                sources = event["sources"]
            if "meta" in event:
                meta = event["meta"]
        return {"content": content, "sources": sources, "meta": meta}


def _sse_indexer(gen):
    """인덱서 async generator를 SSE 스트림으로 변환."""
    async def stream():
        try:
            async for event in gen:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'event': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/index/code")
async def index_code(req: IndexCodeRequest):
    """Git 레포 인덱싱 (1차 — 코드 위키 기반)."""
    return _sse_indexer(index_git_repo(
        git_url=req.git_url,
        project_id=req.project_id,
        branch=req.branch,
        clean_first=True,
    ))


@app.post("/index/wiki")
async def index_wiki(req: IndexWikiRequest):
    """Supabase wiki_docs 인덱싱 (2차)."""
    return _sse_indexer(index_wiki_docs(project_id=req.project_id))


@app.post("/index/task")
async def index_task(req: IndexWikiRequest):
    """Supabase tasks 인덱싱."""
    return _sse_indexer(index_tasks(project_id=req.project_id))


@app.post("/index/sprint")
async def index_sprint(req: IndexWikiRequest):
    """Supabase sprints 인덱싱."""
    return _sse_indexer(index_sprints(project_id=req.project_id))


@app.get("/debug/search")
async def debug_search(
    q: str,
    project_id: Optional[str] = None,
    source_types: Optional[str] = None,  # 쉼표 구분 — "code,wiki,task,sprint"
    top_k: int = 10,
):
    """[r94] 진단용 — 질문에 대해 어떤 청크가 어떤 유사도로 검색되는지 직접 확인.

    예: /debug/search?q=칸반%20데이터%20모델&top_k=10
        /debug/search?q=dbUpsertCategory&source_types=code
    """
    if not q.strip():
        raise HTTPException(400, "q (질문) 필수")
    src = [s.strip() for s in source_types.split(",")] if source_types else None
    chunks = await retrieve(query=q, project_id=project_id, source_types=src, top_k=top_k)
    return {
        "query": q,
        "threshold": SIMILARITY_THRESHOLD,
        "count": len(chunks),
        "results": [
            {
                "rank": i + 1,
                "source_type": c.get("source_type"),
                "source_id": c.get("source_id"),
                "source_title": c.get("source_title"),
                "similarity": round(c.get("similarity", 0), 4),
                "content_preview": (c.get("content", "") or "")[:300],
                "token_count": c.get("token_count"),
            }
            for i, c in enumerate(chunks)
        ],
    }


@app.post("/index/cleanup_empty")
async def cleanup_empty(project_id: Optional[str] = None):
    """[r95] 기존 doc_chunks 중 빈 템플릿/플레이스홀더 청크 일괄 삭제 (재인덱싱 없이).

    "여기에 내용을 작성하세요", "요약 설명을 입력하세요" 등 기본 템플릿이 인덱싱돼
    벡터 검색 결과를 오염시키는 문제를 즉시 해결. 재인덱싱(임베딩 재계산) 없이
    DB만 정리해서 시간을 아낌.
    """
    store = get_store()
    # 청크 전수 스캔 — 페이지네이션 (Supabase 기본 limit 1000)
    all_chunks = []
    page = 0
    page_size = 1000
    while True:
        q = store.client.table("doc_chunks").select("id,content,source_title,source_type")
        if project_id:
            q = q.eq("project_id", project_id)
        q = q.range(page * page_size, (page + 1) * page_size - 1)
        res = q.execute()
        rows = res.data or []
        if not rows:
            break
        all_chunks.extend(rows)
        if len(rows) < page_size:
            break
        page += 1
    # 빈 템플릿 판별
    to_delete_ids = []
    samples = []  # 처음 5개 샘플 (UI 표시용)
    for c in all_chunks:
        if _is_empty_template_chunk(c.get("content", "") or "", c.get("source_title", "") or ""):
            to_delete_ids.append(c["id"])
            if len(samples) < 5:
                samples.append({
                    "source_type": c.get("source_type"),
                    "source_title": c.get("source_title"),
                    "preview": (c.get("content", "") or "")[:80],
                })
    # 배치 삭제 (500개씩)
    deleted = 0
    if to_delete_ids:
        BATCH = 500
        for i in range(0, len(to_delete_ids), BATCH):
            ids = to_delete_ids[i:i + BATCH]
            store.client.table("doc_chunks").delete().in_("id", ids).execute()
            deleted += len(ids)
    return {
        "scanned": len(all_chunks),
        "deleted": deleted,
        "remaining": len(all_chunks) - deleted,
        "samples": samples,
    }


@app.delete("/index/all")
async def index_all_delete(source_type: Optional[str] = None, project_id: Optional[str] = None):
    """특정 source_type 또는 project_id의 청크 모두 삭제."""
    store = get_store()
    if source_type:
        deleted = store.delete_by_source(source_type)
    elif project_id:
        deleted = store.delete_by_project(project_id)
    else:
        raise HTTPException(400, "source_type 또는 project_id 중 하나는 필요")
    return {"deleted": deleted}


def _model_installed(target: str, installed: list) -> bool:
    """모델 매칭 헬퍼 — Ollama는 :latest 등 태그를 자동 추가하므로 base name 비교 포함.

    예: target='nomic-embed-text', installed=['nomic-embed-text:latest'] → True
        target='qwen2.5-coder:14b', installed=['qwen2.5-coder:14b'] → True
    """
    if not target:
        return False
    target_base = target.split(":")[0]
    for m in installed:
        if m == target:
            return True
        m_base = m.split(":")[0]
        if m_base == target_base:
            return True
    return False


@app.on_event("startup")
async def startup():
    print("\n" + "═" * 60)
    print(" 🤖 TDA Deep Wiki 백엔드 시작")
    print("═" * 60)
    ollama = get_ollama()
    store = get_store()
    if await ollama.ping():
        models = await ollama.list_models()
        print(f"  🟢 Ollama 연결 OK · 설치된 모델: {len(models)}개")
        # [r92] :latest 태그 자동 매칭 — Ollama 저장명에 태그가 붙어도 정확히 인식
        if not _model_installed(settings.LLM_MODEL, models):
            print(f"  ⚠️  설정된 LLM '{settings.LLM_MODEL}' 가 ollama list에 없음 — pull 필요")
            print(f"      → ollama pull {settings.LLM_MODEL}")
        else:
            print(f"  ✓  LLM '{settings.LLM_MODEL}' 사용 가능")
        if not _model_installed(settings.EMBED_MODEL, models):
            print(f"  ⚠️  임베딩 모델 '{settings.EMBED_MODEL}' 가 없음 — pull 필요")
            print(f"      → ollama pull {settings.EMBED_MODEL}")
        else:
            print(f"  ✓  임베딩 '{settings.EMBED_MODEL}' 사용 가능")
    else:
        print(f"  🔴 Ollama 연결 실패 ({settings.OLLAMA_BASE_URL})")
    if store.ping():
        print(f"  🟢 Supabase 연결 OK")
    else:
        print(f"  🔴 Supabase 연결 실패 — SUPABASE_URL/SERVICE_KEY 확인")
    print(f"  🌐 CORS 허용: {settings.cors_origins_list}")
    # [r99] 인덱싱 설정값 표시 + 비정상 값 경고 (사용자가 .env에서 잘못 설정한 경우 잡아냄)
    print(f"  📐 청크: CHUNK_SIZE={settings.CHUNK_SIZE}, CHUNK_OVERLAP={settings.CHUNK_OVERLAP}, TOP_K={settings.TOP_K}")
    print(f"  📁 파일 크기 제한: MAX_FILE_SIZE_KB={settings.MAX_FILE_SIZE_KB}")
    warnings = []
    if settings.MAX_FILE_SIZE_KB < 1000:
        warnings.append(
            f"⚠️  MAX_FILE_SIZE_KB={settings.MAX_FILE_SIZE_KB} 너무 작음 — public/index.html(~1840KB) 같은 큰 단일 파일이 스킵됩니다.\n"
            f"      → .env 에서 'MAX_FILE_SIZE_KB=4000' 으로 수정 후 재시작 권장"
        )
    if settings.CHUNK_SIZE < 200 or settings.CHUNK_SIZE > 1500:
        warnings.append(
            f"⚠️  CHUNK_SIZE={settings.CHUNK_SIZE} 비정상 — 권장 범위 200~1000 (기본 500).\n"
            f"      너무 크면 의미 응집도 떨어지고, 너무 작으면 컨텍스트 부족."
        )
    if settings.CHUNK_OVERLAP >= settings.CHUNK_SIZE:
        warnings.append(
            f"⚠️  CHUNK_OVERLAP({settings.CHUNK_OVERLAP}) >= CHUNK_SIZE({settings.CHUNK_SIZE}) — 청킹 무한루프 위험"
        )
    for w in warnings:
        print(f"  {w}")
    if not warnings:
        print(f"  ✓  설정 값 정상")
    print("═" * 60 + "\n")
