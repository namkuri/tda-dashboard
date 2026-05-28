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
from rag import chat_stream
from indexer import index_git_repo, index_wiki_docs, index_tasks, index_sprints


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
async def health():
    """서버 + 의존 서비스 상태."""
    ollama = get_ollama()
    store = get_store()
    ollama_ok = await ollama.ping()
    models = await ollama.list_models() if ollama_ok else []
    supabase_ok = store.ping()
    stats = store.stats() if supabase_ok else {}
    return {
        "status": "ok" if (ollama_ok and supabase_ok) else "degraded",
        "model": settings.LLM_MODEL,
        "embed_model": settings.EMBED_MODEL,
        "ollama": "connected" if ollama_ok else "disconnected",
        "ollama_models": models,
        "supabase": "connected" if supabase_ok else "disconnected",
        "chunks": stats,
        "uptime_sec": int(time.time() - START_TIME),
    }


@app.post("/chat")
async def chat(req: ChatRequest):
    """RAG 챗 — SSE 스트리밍 또는 단건 JSON."""
    if not req.messages:
        raise HTTPException(400, "messages가 비어있습니다")

    if req.stream:
        async def gen():
            try:
                async for event in chat_stream(
                    messages=[m.model_dump() for m in req.messages],
                    project_id=req.project_id,
                    model=req.model,
                    include_tasks=req.include_tasks,
                ):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'delta': f'❌ 오류: {e}'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")
    else:
        # 비스트리밍: 전체 응답을 모아서 한 번에
        content = ""
        sources = []
        async for event in chat_stream(
            messages=[m.model_dump() for m in req.messages],
            project_id=req.project_id,
            model=req.model,
            include_tasks=req.include_tasks,
        ):
            if "delta" in event:
                content += event["delta"]
            if "sources" in event:
                sources = event["sources"]
        return {"content": content, "sources": sources}


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
    print("═" * 60 + "\n")
