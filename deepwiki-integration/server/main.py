"""FastAPI 엔트리포인트 — /health, /chat, /index/*."""
import asyncio
import json
import time
from datetime import datetime
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
from wiki_generator import generate_wiki, extend_wiki_page  # [r113] 자동 위키 + [r132] 페이지 확장
from wiki_auditor import audit_wiki  # [r115] 기획 대조 2차 MD
from mindmap_generator import generate_mindmap  # [r196] 문서→마인드맵


app = FastAPI(title="TDA Deep Wiki", version="1.0.0")
START_TIME = time.time()

# [r126] 위키 생성·인덱싱 진행 상태 (LLM 점유 가시화) — 단일 프로세스 글로벌
#   /chat 등 다른 LLM 호출 엔드포인트가 busy 게이트로 이용하고 /health 가 노출.
LLM_BUSY_STATE: Dict[str, Any] = {
    "running": False,        # True면 LLM 점유 중
    "kind": None,            # 'wiki_generate' | 'wiki_audit' | 'index_code' | ...
    "project_id": None,
    "started_at": None,      # epoch sec
    "stage": None,           # 'clone' | 'scan' | 'architecture' | 'generate' | 'overview' | ...
    "current": 0,
    "total": 0,
    "category": None,        # 현재 처리 중 카테고리 제목
    "message": None,         # 사람이 읽을 한국어 진행 메시지
}


def _busy_set(**kwargs):
    LLM_BUSY_STATE.update(kwargs)


def _busy_clear():
    LLM_BUSY_STATE.update({
        "running": False, "kind": None, "project_id": None, "started_at": None,
        "stage": None, "current": 0, "total": 0, "category": None, "message": None,
    })


# [r133] 마지막 wiki 생성 진단 로그 — SSE 죽어서 사용자가 못 본 에러를 retrieve 가능
LAST_GENERATION_LOG: Dict[str, Any] = {
    "kind": None,
    "project_id": None,
    "started_at": None,
    "ended_at": None,
    "events": [],         # 모든 SSE 이벤트 (warn / error / info / done 등)
    "summary": None,
}


def _diag_clear():
    LAST_GENERATION_LOG.update({"kind": None, "project_id": None, "started_at": None, "ended_at": None, "events": [], "summary": None})


def _busy_human() -> str:
    """현재 busy 상태를 한국어 한 줄로."""
    if not LLM_BUSY_STATE["running"]:
        return ""
    kind = LLM_BUSY_STATE.get("kind") or "작업"
    kind_label = {
        "wiki_generate": "Deep Wiki 자동 생성",
        "wiki_audit": "기획 대조 보고서(2차) 생성",
        "mindmap_generate": "마인드맵 생성",  # [r196]
        "index_code": "코드 인덱싱",
        "index_wiki": "위키 인덱싱",
        "index_task": "태스크 인덱싱",
        "index_sprint": "스프린트 인덱싱",
    }.get(kind, kind)
    stage = LLM_BUSY_STATE.get("stage") or "?"
    cur = LLM_BUSY_STATE.get("current") or 0
    tot = LLM_BUSY_STATE.get("total") or 0
    cat = LLM_BUSY_STATE.get("category")
    elapsed = int(time.time() - (LLM_BUSY_STATE.get("started_at") or time.time()))
    prog = f"{cur}/{tot}" if tot else "진행 중"
    parts = [f"🔄 **{kind_label}** 진행 중", f"단계: `{stage}`", f"진행률: `{prog}`"]
    if cat:
        parts.append(f"현재: `{cat}`")
    parts.append(f"경과: `{elapsed}s`")
    return " · ".join(parts)

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


# [r113] Deep Wiki 자동 위키 생성 요청
class WikiGenerateRequest(BaseModel):
    git_url: str
    project_id: str
    branch: str = "main"
    model: Optional[str] = None
    mode: str = "full"  # [r146] 'full' | 'incremental' (재시도/누락 채우기)


# [r115] 기획 대조 감사 요청
class WikiAuditRequest(BaseModel):
    project_id: str
    model: Optional[str] = None
    canon_ids: Optional[List[str]] = None  # [r150] 대조 대상(기획) 직접 선택 — 없으면 전체


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
    # [r126] LLM 점유 상태 노출
    out["llm_busy"] = dict(LLM_BUSY_STATE)
    if LLM_BUSY_STATE["running"]:
        out["llm_busy_human"] = _busy_human()
    return out


@app.post("/chat")
async def chat(req: ChatRequest):
    """RAG 챗 — SSE 스트리밍 또는 단건 JSON."""
    if not req.messages:
        raise HTTPException(400, "messages가 비어있습니다")

    # [r126] 위키/인덱싱이 진행 중이면 — 같은 Ollama 모델을 다중 동시 호출하면
    #   양쪽 모두 매우 느려지거나 타임아웃. 명확한 안내 후 차단.
    if LLM_BUSY_STATE["running"]:
        busy_msg = (
            "⚠ **로컬 LLM이 다른 작업으로 점유 중입니다**\n\n"
            + _busy_human()
            + "\n\n"
            "Ollama 서버는 한 번에 하나의 무거운 LLM 호출만 안정적으로 처리합니다. "
            "지금 질문을 보내면:\n"
            "- ❌ AI Agent 응답이 매우 느림(분 단위) 또는 타임아웃\n"
            "- ❌ 진행 중인 위키 생성도 함께 지연·실패 위험\n\n"
            "**권장 조치:**\n"
            "1. Deep Wiki 페이지로 가서 좌측 진행률(`🔄 ...`) 확인\n"
            "2. 모든 page_done 이벤트가 끝날 때까지 대기 (남은 카테고리 수 × 30~120초)\n"
            "3. 완료되면 다시 질문하세요 — 즉시 응답됩니다\n\n"
            "_(이 메시지는 백엔드 연결 실패가 아니라 의도적 게이트입니다.)_"
        )
        if req.stream:
            async def busy_gen():
                yield f"data: {json.dumps({'delta': busy_msg, 'busy': True, 'busy_state': dict(LLM_BUSY_STATE)}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(busy_gen(), media_type="text/event-stream")
        return {"content": busy_msg, "sources": [], "meta": {"busy": True, "busy_state": dict(LLM_BUSY_STATE)}}

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


def _sse_indexer(gen, *, busy_kind: Optional[str] = None, busy_project: Optional[str] = None):
    """인덱서/제너레이터 async generator를 SSE 스트림으로 변환.

    [r126] busy_kind 가 주어지면 LLM_BUSY_STATE 를 업데이트해 /chat 게이트가 알 수 있게 한다.
    event 의 stage/current/total/category 를 자동으로 state 에 흘림.
    [r133] busy_kind 가 wiki_generate/wiki_extend/wiki_audit 이면 모든 이벤트를 LAST_GENERATION_LOG 에 보존
      → SSE 끊겨도 /diag/last_generation 으로 사용자가 무엇이 일어났는지 확인 가능.
    """
    capture_kinds = ("wiki_generate", "wiki_extend", "wiki_audit")
    do_capture = busy_kind in capture_kinds

    async def stream():
        if busy_kind:
            _busy_set(running=True, kind=busy_kind, project_id=busy_project, started_at=time.time(),
                      stage="시작", current=0, total=0, category=None,
                      message=f"{busy_kind} 시작…")
        if do_capture:
            _diag_clear()
            LAST_GENERATION_LOG.update({
                "kind": busy_kind,
                "project_id": busy_project,
                "started_at": datetime.now().isoformat(),
                "events": [],
            })
        try:
            async for event in gen:
                # state 업데이트 — event 키에 따라 자동 매핑
                if busy_kind:
                    et = event.get("event")
                    if et == "stage":
                        _busy_set(stage=event.get("stage") or LLM_BUSY_STATE["stage"],
                                  message=event.get("message") or LLM_BUSY_STATE["message"])
                    elif et == "progress":
                        _busy_set(current=event.get("current") or 0,
                                  total=event.get("total") or 0,
                                  category=event.get("category") or LLM_BUSY_STATE["category"])
                    elif et in ("done", "error"):
                        pass  # finally 에서 clear
                if do_capture:
                    LAST_GENERATION_LOG["events"].append({
                        "ts": datetime.now().isoformat(),
                        **event,
                    })
                    # 너무 크지 않게 — 마지막 500 이벤트만
                    if len(LAST_GENERATION_LOG["events"]) > 500:
                        LAST_GENERATION_LOG["events"] = LAST_GENERATION_LOG["events"][-500:]
                    if event.get("event") == "done":
                        LAST_GENERATION_LOG["summary"] = {k: v for k, v in event.items() if k != "event"}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            import traceback as _tb
            err_tb = _tb.format_exc()
            print(f"[main] ❌ SSE generator exception\n{err_tb}")
            err_ev = {"event": "error", "message": str(e), "traceback": err_tb.splitlines()[-8:]}
            if do_capture:
                LAST_GENERATION_LOG["events"].append({"ts": datetime.now().isoformat(), **err_ev})
            yield f"data: {json.dumps(err_ev, ensure_ascii=False)}\n\n"
        finally:
            if do_capture:
                LAST_GENERATION_LOG["ended_at"] = datetime.now().isoformat()
            if busy_kind:
                _busy_clear()
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
    ), busy_kind="index_code", busy_project=req.project_id)


@app.post("/index/wiki")
async def index_wiki(req: IndexWikiRequest):
    """Supabase wiki_docs 인덱싱 (2차)."""
    return _sse_indexer(index_wiki_docs(project_id=req.project_id),
                        busy_kind="index_wiki", busy_project=req.project_id)


@app.post("/index/task")
async def index_task(req: IndexWikiRequest):
    """Supabase tasks 인덱싱."""
    return _sse_indexer(index_tasks(project_id=req.project_id),
                        busy_kind="index_task", busy_project=req.project_id)


@app.post("/index/sprint")
async def index_sprint(req: IndexWikiRequest):
    """Supabase sprints 인덱싱."""
    return _sse_indexer(index_sprints(project_id=req.project_id),
                        busy_kind="index_sprint", busy_project=req.project_id)


# [r130] 증분 자동 동기화 — wiki+task+sprint 를 since 이후로만 재 인덱싱
class IndexSyncRequest(BaseModel):
    project_id: Optional[str] = None
    since: Optional[str] = None  # ISO datetime (예: "2026-05-29T11:30:00Z")
    include: List[str] = ["wiki", "task", "sprint"]


@app.post("/index/sync")
async def index_sync(req: IndexSyncRequest):
    """[r130] 증분 동기화 — Supabase 의 wiki_docs / tasks / sprints 중 updated_at >= since 인 것만 재 임베딩.

    프론트가 lastSyncAt 을 localStorage 로 관리하고, 헬스체크 성공 직후 + 주기적으로 호출.
    Ollama 가 다른 작업으로 점유 중이면 거부 (busy 게이트).
    응답: SSE — phase 별 진행률 + 총 결과 요약.
    """
    if LLM_BUSY_STATE["running"]:
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'busy', 'message': _busy_human()}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")

    async def combined():
        _busy_set(running=True, kind="index_sync", project_id=req.project_id, started_at=time.time(),
                  stage="시작", current=0, total=len(req.include), category=None,
                  message=f"증분 동기화 시작 (since={req.since or 'full'})")
        try:
            phase_idx = 0
            summary: Dict[str, Any] = {"since": req.since, "phases": {}}
            for kind in req.include:
                phase_idx += 1
                _busy_set(stage=kind, current=phase_idx, total=len(req.include), category=kind)
                yield {"event": "phase_start", "kind": kind, "current": phase_idx, "total": len(req.include)}
                if kind == "wiki":
                    gen = index_wiki_docs(project_id=req.project_id, since=req.since)
                elif kind == "task":
                    gen = index_tasks(project_id=req.project_id, since=req.since)
                elif kind == "sprint":
                    gen = index_sprints(project_id=req.project_id, since=req.since)
                else:
                    yield {"event": "warn", "message": f"알 수 없는 kind: {kind}"}
                    continue
                phase_result = {"chunks_inserted": 0, "skipped_empty": 0, "total_rows": 0}
                async for ev in gen:
                    # 하위 이벤트 그대로 흘리되, prefix 로 kind 표시
                    ev2 = {**ev, "_kind": kind}
                    yield ev2
                    if ev.get("event") == "start":
                        phase_result["total_rows"] = ev.get("total_docs") or ev.get("total_tasks") or ev.get("total_sprints") or 0
                    elif ev.get("event") == "done":
                        phase_result["chunks_inserted"] = ev.get("chunks_inserted", 0)
                        phase_result["skipped_empty"] = ev.get("skipped_empty", 0)
                summary["phases"][kind] = phase_result
                yield {"event": "phase_done", "kind": kind, **phase_result}
            # 마지막 종합
            now_iso = datetime.now().isoformat()
            summary["completed_at"] = now_iso
            total_chunks = sum(p.get("chunks_inserted", 0) for p in summary["phases"].values())
            yield {"event": "done", "summary": summary, "total_chunks_inserted": total_chunks, "next_since": now_iso}
        except Exception as e:
            yield {"event": "error", "message": str(e)}
        finally:
            _busy_clear()

    async def stream():
        async for ev in combined():
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────
# [r113] Deep Wiki — 자동 위키 페이지 생성·조회
# ─────────────────────────────────────────────

@app.post("/wiki/generate")
async def wiki_generate(req: WikiGenerateRequest):
    """Git 레포 → LLM 자동 위키 페이지 N개 생성. SSE 진행률 스트리밍."""
    # [r126] 다른 LLM 작업이 진행 중이면 거부 (동시 호출 시 둘 다 망가짐)
    if LLM_BUSY_STATE["running"]:
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업이 진행 중입니다: ' + _busy_human()}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    # [r146] incremental 모드면 clean_first=False (기존 클론 재사용해 git pull 효과)
    incremental = (req.mode == "incremental")
    return _sse_indexer(generate_wiki(
        git_url=req.git_url,
        project_id=req.project_id,
        branch=req.branch,
        clean_first=not incremental,
        model=req.model,
        mode=req.mode,
    ), busy_kind="wiki_generate", busy_project=req.project_id)


# ─────────────────────────────────────────────
# [r196] 문서 → 마인드맵 자동 생성
# ─────────────────────────────────────────────

class MindmapDoc(BaseModel):
    title: str
    content: str


class MindmapGenerateRequest(BaseModel):
    docs: List[MindmapDoc]
    mode: str = "auto"           # 'auto' | 'single' | 'multi'
    model: Optional[str] = None
    project_id: Optional[str] = None  # busy 추적용(선택)


@app.post("/mindmap/generate")
async def mindmap_generate(req: MindmapGenerateRequest):
    """문서 1개 또는 여러 개 → LLM으로 마인드맵 JSON 생성. SSE 진행률 스트리밍.

    응답 done 이벤트의 diagram payload를 그대로 wiki_docs.meta.diagram에 저장하면 그래프 뷰에서 즉시 렌더됨.
    """
    if LLM_BUSY_STATE["running"]:
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업이 진행 중입니다: ' + _busy_human()}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    docs = [{"title": d.title, "content": d.content} for d in (req.docs or [])]
    return _sse_indexer(generate_mindmap(
        docs=docs,
        model=req.model,
        mode=req.mode,
    ), busy_kind="mindmap_generate", busy_project=req.project_id)


@app.get("/wiki/pages")
async def wiki_pages(
    project_id: str,
    repo_name: Optional[str] = None,
    generation_id: Optional[str] = None,
    only_latest: bool = True,
):
    """[r121] 프로젝트의 자동 생성 위키 페이지 목록.

    필터:
      - repo_name: 특정 레포만
      - generation_id: 특정 생성 회차만
      - only_latest: True면 is_latest=true만 (기본). False면 모든 버전.
    """
    if not project_id:
        raise HTTPException(400, "project_id 필수")
    store = get_store()
    try:
        q = (
            store.client.table("deep_wiki_pages")
            .select("id,slug,title,parent_slug,sort_order,summary,git_url,git_commit,updated_at,meta,repo_name,generation_id,is_latest")
            .eq("project_id", project_id)
        )
        if repo_name:
            q = q.eq("repo_name", repo_name)
        if generation_id:
            q = q.eq("generation_id", generation_id)
        elif only_latest:
            # 003 SQL 미실행이면 is_latest 컬럼 없음 → 폴백
            try:
                q = q.eq("is_latest", True)
            except Exception:
                pass
        q = q.order("sort_order")
        res = q.execute()
        return {"pages": res.data or [], "count": len(res.data or [])}
    except Exception as e:
        # is_latest 컬럼 없어서 실패하면 폴백 — 전체 조회
        msg = str(e)
        if "is_latest" in msg or "repo_name" in msg or "generation_id" in msg:
            try:
                fallback = (
                    store.client.table("deep_wiki_pages")
                    .select("*")
                    .eq("project_id", project_id)
                    .order("sort_order")
                    .execute()
                )
                return {"pages": fallback.data or [], "count": len(fallback.data or []), "warn": "003_wiki_versioning.sql 미실행 — 버전 컬럼 없이 전체 페이지 표시"}
            except Exception as e2:
                return {"pages": [], "count": 0, "error": f"조회 실패: {e2}"}
        return {"pages": [], "count": 0, "error": f"deep_wiki_pages 조회 실패: {e}"}


@app.get("/wiki/repos")
async def wiki_repos(project_id: str):
    """[r121] 프로젝트의 위키 레포·버전 목록 (좌측 그룹화 UI용)."""
    if not project_id:
        raise HTTPException(400, "project_id 필수")
    store = get_store()
    try:
        res = (
            store.client.table("deep_wiki_pages")
            .select("repo_name,generation_id,git_url,git_commit,is_latest,updated_at")
            .eq("project_id", project_id)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        return {"repos": [], "error": f"조회 실패: {e}"}
    # repo_name 별로 generation 묶기
    repos: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        rname = r.get("repo_name") or "(unknown)"
        gen = r.get("generation_id") or "legacy"
        if rname not in repos:
            repos[rname] = {"repo_name": rname, "git_url": r.get("git_url"), "generations": {}}
        if gen not in repos[rname]["generations"]:
            repos[rname]["generations"][gen] = {
                "generation_id": gen,
                "git_commit": r.get("git_commit"),
                "is_latest": bool(r.get("is_latest")),
                "updated_at": r.get("updated_at"),
                "page_count": 0,
            }
        repos[rname]["generations"][gen]["page_count"] += 1
    # 정리: generations를 list로 변환 + 최신순 정렬
    out = []
    for rname, info in repos.items():
        gens = sorted(info["generations"].values(), key=lambda g: g.get("updated_at") or "", reverse=True)
        out.append({
            "repo_name": rname,
            "git_url": info.get("git_url"),
            "generations": gens,
            "latest_gen": gens[0]["generation_id"] if gens else None,
        })
    out.sort(key=lambda r: r["repo_name"])
    return {"repos": out, "count": len(out)}


@app.get("/wiki/page")
async def wiki_page(project_id: str, slug: str):
    """단일 위키 페이지 본문 조회."""
    if not project_id or not slug:
        raise HTTPException(400, "project_id, slug 필수")
    store = get_store()
    try:
        res = (
            store.client.table("deep_wiki_pages")
            .select("*")
            .eq("project_id", project_id)
            .eq("slug", slug)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            raise HTTPException(404, f"슬러그 '{slug}' 페이지 없음")
        return rows[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"조회 실패: {e}")


@app.delete("/wiki/pages")
async def wiki_pages_delete(project_id: str):
    """프로젝트의 모든 자동 생성 위키 페이지 삭제."""
    if not project_id:
        raise HTTPException(400, "project_id 필수")
    store = get_store()
    try:
        res = store.client.table("deep_wiki_pages").delete().eq("project_id", project_id).execute()
        return {"deleted": len(res.data or [])}
    except Exception as e:
        raise HTTPException(500, f"삭제 실패: {e}")


# ─────────────────────────────────────────────
# [r115] Phase C — 기획 대조 감사 보고서
# ─────────────────────────────────────────────

# [r132] 기존 페이지 확장 — 새 섹션 LLM 으로 추가
class WikiExtendRequest(BaseModel):
    project_id: str
    slug: str
    extension_type: str  # 'deep_dive' | 'performance' | 'pitfalls' | 'examples' | 'testing' | 'custom'
    custom_prompt: Optional[str] = None
    model: Optional[str] = None


@app.get("/diag/last_generation")
async def diag_last_generation():
    """[r133] 마지막 wiki 생성/감사/확장 작업의 모든 SSE 이벤트 + traceback 반환.

    SSE 연결이 끊겨서 사용자가 에러를 못 본 경우 retrieve 용도. 새로고침 직후 자동 호출 권장.
    """
    log = dict(LAST_GENERATION_LOG)
    # 요약: 이벤트별 카운트 + 에러/경고만 추출
    events = log.get("events") or []
    errors = [e for e in events if e.get("event") == "error"]
    warns = [e for e in events if e.get("event") == "warn"]
    stages = [e for e in events if e.get("event") == "stage"]
    return {
        **log,
        "stats": {
            "total_events": len(events),
            "errors": len(errors),
            "warns": len(warns),
            "stages": len(stages),
        },
        "errors": errors[-10:],
        "warns": warns[-20:],
    }


@app.post("/wiki/extend")
async def wiki_extend(req: WikiExtendRequest):
    """[r132] 기존 위키 페이지에 LLM 으로 새 섹션 추가. SSE 진행률 + 본문에 append."""
    if LLM_BUSY_STATE["running"]:
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업이 진행 중입니다: ' + _busy_human()}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    return _sse_indexer(
        extend_wiki_page(
            project_id=req.project_id,
            slug=req.slug,
            extension_type=req.extension_type,
            custom_prompt=req.custom_prompt,
            model=req.model,
        ),
        busy_kind="wiki_extend",
        busy_project=req.project_id,
    )


@app.post("/wiki/audit")
async def wiki_audit(req: WikiAuditRequest):
    """canon 문서 × 1차 자동 위키 대조 → 2차 일치도 보고서 생성."""
    if LLM_BUSY_STATE["running"]:
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업이 진행 중입니다: ' + _busy_human()}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    return _sse_indexer(audit_wiki(project_id=req.project_id, model=req.model, canon_ids=req.canon_ids),
                        busy_kind="wiki_audit", busy_project=req.project_id)


@app.get("/wiki/audits")
async def wiki_audits(project_id: str):
    """프로젝트의 감사 보고서 목록."""
    if not project_id:
        raise HTTPException(400, "project_id 필수")
    store = get_store()
    try:
        res = (
            store.client.table("deep_wiki_audits")
            .select("id,title,summary,match_score,related_pages,related_canons,findings,updated_at")
            .eq("project_id", project_id)
            .order("match_score", desc=False)  # 점수 낮은 것 = 문제 많은 것 먼저
            .execute()
        )
        return {"audits": res.data or [], "count": len(res.data or [])}
    except Exception as e:
        return {"audits": [], "count": 0, "error": f"deep_wiki_audits 테이블 미생성 또는 조회 실패: {e}"}


@app.get("/wiki/audit/{audit_id}")
async def wiki_audit_get(audit_id: str, project_id: str):
    """단일 감사 보고서 상세."""
    if not project_id or not audit_id:
        raise HTTPException(400, "audit_id, project_id 필수")
    store = get_store()
    try:
        res = (
            store.client.table("deep_wiki_audits")
            .select("*")
            .eq("project_id", project_id)
            .eq("id", audit_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            raise HTTPException(404, f"audit '{audit_id}' 없음")
        return rows[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"조회 실패: {e}")


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
