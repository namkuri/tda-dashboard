"""FastAPI 엔트리포인트 — /health, /chat, /index/*."""
import asyncio
import json
import time
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from config import settings
from ollama_client import get_ollama
from supabase_store import get_store
# [r226] Gemini 클라이언트 (선택적 — httpx 만 있으면 동작)
try:
    from gemini_client import get_gemini, FREE_TIER_MODELS as GEMINI_FREE_MODELS
    _HAS_GEMINI = True
except Exception:
    _HAS_GEMINI = False
    GEMINI_FREE_MODELS = []
from rag import chat_stream as legacy_chat_stream  # [r108] 폴백용
from agent import run as agent_run  # [r108] Tool Use 에이전트
from retriever import retrieve, SIMILARITY_THRESHOLD
from indexer import index_git_repo, index_wiki_docs, index_tasks, index_sprints, _is_empty_template_chunk
from wiki_generator import generate_wiki, extend_wiki_page  # [r113] 자동 위키 + [r132] 페이지 확장
from wiki_auditor import audit_wiki  # [r115] 기획 대조 2차 MD
from mindmap_generator import generate_mindmap  # [r196] 문서→마인드맵
# [r208] 신규 엔티티 인덱서 — issue/event/asset/review/bug/wbs
from indexer_extras import (
    index_issues, index_calendar_events, index_assets,
    index_reviews, index_bug_reports, index_wbs_nodes,
)


app = FastAPI(title="TDA Deep Wiki", version="1.0.0")
START_TIME = time.time()
# [r209] 백엔드 코드 리비전 — /health 응답에 포함. 프론트(_AHUB_FRONT_REV)와
# 비교해 "코드 변경 후 서버 미재시작"을 자동 감지·경고.
SERVER_REVISION = "r266"

# [r226] Gemini 라우터 — 순환 import 방지 위해 llm_router 모듈에서 가져옴.
from llm_router import GEMINI_CONFIG, get_llm, is_gemini_model

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
    # [r225] heartbeat — 갱신될 때마다 last_update. stale 판정용.
    LLM_BUSY_STATE["last_update"] = time.time()


def _busy_clear():
    LLM_BUSY_STATE.update({
        "running": False, "kind": None, "project_id": None, "started_at": None,
        "stage": None, "current": 0, "total": 0, "category": None, "message": None,
        "last_update": None,
    })


# [r225] busy stale 자동 해제 — client disconnect 후 finally 가 안 돌아 busy 가
# 영구 점유되는 버그 방어. 90초 동안 이벤트 갱신 없으면 죽은 작업으로 간주해 해제.
_BUSY_STALE_SEC = 90


def _busy_active() -> bool:
    """진짜 LLM 점유 중인지 — stale 이면 자동 해제 후 False."""
    if not LLM_BUSY_STATE.get("running"):
        return False
    last = LLM_BUSY_STATE.get("last_update") or LLM_BUSY_STATE.get("started_at") or 0
    if time.time() - last > _BUSY_STALE_SEC:
        print(f"[main] ⚠ busy stale 자동 해제 (kind={LLM_BUSY_STATE.get('kind')}, "
              f"{int(time.time() - last)}s 무응답)")
        _busy_clear()
        return False
    return True


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
        # [r208] 신규
        "index_issue": "이슈 인덱싱",
        "index_event": "일정 인덱싱",
        "index_asset": "에셋 인덱싱",
        "index_review": "리뷰 인덱싱",
        "index_bug": "버그 리포트 인덱싱",
        "index_wbs": "작업구조화 인덱싱",
        "index_sync": "전체 증분 동기화",
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
    # [r214] 현재 사용자 — 개인 문서 비공개 가드(meta.owner==user_id) + "내 카드" 필터
    user_id: Optional[str] = None


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
        # [r209] 백엔드 코드 리비전 마커 — 프론트가 자기 리비전과 대조해
        # "코드 변경 후 백엔드 미재시작" 같은 상황을 자동 감지·경고.
        "server_revision": SERVER_REVISION,
    }
    if verbose and supabase_ok:
        out["top_sources"] = store.top_sources(limit=15)
    # [r126] LLM 점유 상태 노출
    out["llm_busy"] = dict(LLM_BUSY_STATE)
    if _busy_active():
        out["llm_busy_human"] = _busy_human()
    return out


@app.post("/chat")
async def chat(req: ChatRequest):
    """RAG 챗 — SSE 스트리밍 또는 단건 JSON."""
    if not req.messages:
        raise HTTPException(400, "messages가 비어있습니다")

    # [r126] 위키/인덱싱이 진행 중이면 — 같은 Ollama 모델을 다중 동시 호출하면
    #   양쪽 모두 매우 느려지거나 타임아웃. 명확한 안내 후 차단.
    if _busy_active():
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
                    user_id=req.user_id,  # [r214]
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
        # [r209] 첫 이벤트로 서버 리비전 노출 — 프론트가 미일치 자동 감지
        yield f"data: {json.dumps({'event': 'server_info', 'server_revision': SERVER_REVISION, 'kind': busy_kind}, ensure_ascii=False)}\n\n"
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
                    # [r225] 모든 이벤트마다 heartbeat — stale 오판 방지
                    LLM_BUSY_STATE["last_update"] = time.time()
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


# [r208] 신규 엔티티 인덱싱 — issue/event/asset/review/bug/wbs
@app.post("/index/issue")
async def index_issue_ep(req: IndexWikiRequest):
    return _sse_indexer(index_issues(project_id=req.project_id),
                        busy_kind="index_issue", busy_project=req.project_id)


@app.post("/index/event")
async def index_event_ep(req: IndexWikiRequest):
    return _sse_indexer(index_calendar_events(project_id=req.project_id),
                        busy_kind="index_event", busy_project=req.project_id)


@app.post("/index/asset")
async def index_asset_ep(req: IndexWikiRequest):
    return _sse_indexer(index_assets(project_id=req.project_id),
                        busy_kind="index_asset", busy_project=req.project_id)


@app.post("/index/review")
async def index_review_ep(req: IndexWikiRequest):
    return _sse_indexer(index_reviews(project_id=req.project_id),
                        busy_kind="index_review", busy_project=req.project_id)


@app.post("/index/bug")
async def index_bug_ep(req: IndexWikiRequest):
    return _sse_indexer(index_bug_reports(project_id=req.project_id),
                        busy_kind="index_bug", busy_project=req.project_id)


@app.post("/index/wbs")
async def index_wbs_ep(req: IndexWikiRequest):
    return _sse_indexer(index_wbs_nodes(project_id=req.project_id),
                        busy_kind="index_wbs", busy_project=req.project_id)


# [r130] 증분 자동 동기화 — wiki+task+sprint 를 since 이후로만 재 인덱싱
class IndexSyncRequest(BaseModel):
    project_id: Optional[str] = None
    since: Optional[str] = None  # ISO datetime (예: "2026-05-29T11:30:00Z")
    include: List[str] = ["wiki", "task", "sprint", "wbs", "issue", "event", "asset", "review"]  # [r208] 신규 4 추가


@app.post("/index/sync")
async def index_sync(req: IndexSyncRequest):
    """[r130] 증분 동기화 — Supabase 의 wiki_docs / tasks / sprints 중 updated_at >= since 인 것만 재 임베딩.

    프론트가 lastSyncAt 을 localStorage 로 관리하고, 헬스체크 성공 직후 + 주기적으로 호출.
    Ollama 가 다른 작업으로 점유 중이면 거부 (busy 게이트).
    응답: SSE — phase 별 진행률 + 총 결과 요약.
    """
    if _busy_active():
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
                # [r209] 한 phase 실패가 전체 sync 를 망치지 않도록 격리
                gen = None
                try:
                    if kind == "wiki":
                        gen = index_wiki_docs(project_id=req.project_id, since=req.since)
                    elif kind == "task":
                        gen = index_tasks(project_id=req.project_id, since=req.since)
                    elif kind == "sprint":
                        gen = index_sprints(project_id=req.project_id, since=req.since)
                    # [r208] 신규 엔티티
                    elif kind == "issue":
                        gen = index_issues(project_id=req.project_id, since=req.since)
                    elif kind == "event":
                        gen = index_calendar_events(project_id=req.project_id, since=req.since)
                    elif kind == "asset":
                        gen = index_assets(project_id=req.project_id, since=req.since)
                    elif kind == "review":
                        gen = index_reviews(project_id=req.project_id, since=req.since)
                    elif kind == "bug":
                        gen = index_bug_reports(project_id=req.project_id, since=req.since)
                    elif kind == "wbs":
                        gen = index_wbs_nodes(project_id=req.project_id, since=req.since)
                    else:
                        yield {"event": "warn", "message": f"알 수 없는 kind: {kind}"}
                        yield {"event": "phase_failed", "kind": kind, "reason": "unknown_kind"}
                        continue
                except Exception as e:
                    yield {"event": "phase_failed", "kind": kind, "reason": str(e)}
                    summary["phases"][kind] = {"chunks_inserted": 0, "skipped_empty": 0, "total_rows": 0, "error": str(e)}
                    continue
                phase_result = {"chunks_inserted": 0, "skipped_empty": 0, "total_rows": 0}
                phase_error: Optional[str] = None
                try:
                    async for ev in gen:
                        ev2 = {**ev, "_kind": kind}
                        yield ev2
                        if ev.get("event") == "start":
                            phase_result["total_rows"] = ev.get("total_docs") or ev.get("total_tasks") or ev.get("total_sprints") or 0
                        elif ev.get("event") == "done":
                            phase_result["chunks_inserted"] = ev.get("chunks_inserted", 0)
                            phase_result["skipped_empty"] = ev.get("skipped_empty", 0)
                except Exception as e:
                    phase_error = str(e)
                    yield {"event": "warn", "message": f"{kind} 단계 예외 — 다음 단계로 진행: {e}", "_kind": kind}
                if phase_error:
                    summary["phases"][kind] = {**phase_result, "error": phase_error}
                    yield {"event": "phase_failed", "kind": kind, "reason": phase_error}
                else:
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
        # [r209] 첫 이벤트로 서버 리비전 노출 — 프론트가 미일치 자동 감지
        yield f"data: {json.dumps({'event': 'server_info', 'server_revision': SERVER_REVISION, 'kind': 'index_sync'}, ensure_ascii=False)}\n\n"
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
    if _busy_active():
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
    id: Optional[str] = None  # [r217] 노드 클릭 explain 시 wiki_docs 조회용
    title: str
    content: str
    attachment_urls: List[str] = []  # [r202] HTML/DOCX/TXT/MD 등 첨부 파일 URL


class MindmapGenerateRequest(BaseModel):
    docs: List[MindmapDoc]
    mode: str = "auto"           # 'auto' | 'single' | 'multi'
    model: Optional[str] = None
    project_id: Optional[str] = None  # busy 추적용(선택)


# [r217] 마인드맵 노드 클릭 → AI 설명 요청
class MindmapSourceOverride(BaseModel):
    id: Optional[str] = None
    title: str
    content: str  # 프론트가 htmlEmbeds 풀어서 보낸 텍스트


class MindmapExplainRequest(BaseModel):
    project_id: Optional[str] = None
    central: str = ""
    diagram_branches: List[Dict[str, Any]] = []   # 원본 LLM branches 트리(파싱 결과)
    node_title: str
    node_path: Optional[List[str]] = None         # 부모 title 체인(있으면 정확 매칭)
    source_doc_ids: List[str] = []                # doc.meta.mindmap_sources 의 id 목록
    # [r220] 프론트가 미리 expand 한 본문 — wiki_docs.content 가 토큰만 있을 때 우선 사용
    source_docs_overrides: List[MindmapSourceOverride] = []
    # [r222] 자유 질문 — 노드 컨텍스트는 유지하고 사용자가 직접 던지는 질문
    user_question: Optional[str] = None
    model: Optional[str] = None


@app.post("/mindmap/generate")
async def mindmap_generate(req: MindmapGenerateRequest):
    """문서 1개 또는 여러 개 → LLM으로 마인드맵 JSON 생성. SSE 진행률 스트리밍.

    응답 done 이벤트의 diagram payload를 그대로 wiki_docs.meta.diagram에 저장하면 그래프 뷰에서 즉시 렌더됨.
    """
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업이 진행 중입니다: ' + _busy_human()}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    docs = [{"id": d.id, "title": d.title, "content": d.content, "attachment_urls": (d.attachment_urls or [])} for d in (req.docs or [])]
    return _sse_indexer(generate_mindmap(
        docs=docs,
        model=req.model,
        mode=req.mode,
    ), busy_kind="mindmap_generate", busy_project=req.project_id)


# [r217] 마인드맵 노드 클릭 → AI 설명 (NotebookLM 스타일)
from mindmap_explain import explain_node as _mm_explain_node


@app.post("/mindmap/explain")
async def mindmap_explain(req: MindmapExplainRequest):
    """마인드맵의 특정 노드를 클릭했을 때 LLM 이 본문 인용으로 설명 + 후속 질문 3개.

    SSE 이벤트:
      stage/sources/delta/followups/done
    프론트:
      - delta 텍스트를 누적해 마크다운 렌더
      - 텍스트의 [^N] 각주 → 클릭 시 sources[N-1] popover
      - followups 3개 → 클릭 시 그 query 로 재호출
    """
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업이 진행 중입니다: ' + _busy_human()}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    gen = _mm_explain_node(
        project_id=req.project_id,
        diagram_branches=req.diagram_branches or [],
        central=req.central or "마인드맵",
        node_title=req.node_title,
        node_path=req.node_path,
        source_doc_ids=req.source_doc_ids or [],
        source_overrides=[s.model_dump() for s in (req.source_docs_overrides or [])],
        user_question=req.user_question,
        model=req.model,
    )
    return _sse_indexer(gen, busy_kind="mindmap_explain", busy_project=req.project_id)


# ─────────────────────────────────────────────
# [r223] 연구 정련소 (Research Refinery)
# [r224] import 안전망 — refinery 모듈 import 실패해도 백엔드 전체는 살아남게.
#        실패 시 _REFINERY_OK=False, 각 엔드포인트가 503 + 명확한 사유 반환.
# ─────────────────────────────────────────────
_REFINERY_OK = True
_REFINERY_ERR = None
try:
    from refinery import _session_store as _rfs
    from refinery._author_guard import require_author
    from refinery.decomposer import decompose as _rfs_decompose
    from refinery.classifier import classify_suggest as _rfs_classify
    from refinery.similar_nodes import find_similar_groups as _rfs_similar
    from refinery.composer import (
        build_tree_skeleton as _rfs_build_tree,
        compose_file_body as _rfs_compose_body,
        compose_vault_list_md as _rfs_compose_vault_list,
        compose_changelog_md as _rfs_compose_changelog,
    )
    from refinery.linker import link_files as _rfs_link
    from refinery.work_proposer import propose_work as _rfs_propose
    from refinery.apply_ops import apply_tree as _rfs_apply_tree, apply_work as _rfs_apply_work, apply_stream as _rfs_apply_stream
    from refinery.structure import analyze_structure as _rfs_analyze  # [r246] 로직 우선 분해
    from refinery.intake import propose_intake as _rfs_intake  # [r247] 들여오기 추천
    from refinery.wiki_compose import compose_wiki_body as _rfs_wiki_body  # [r247] 위키 본문 작성
    from refinery.pipeline import derive_stream as _rfs_derive_stream  # [r253] 도출 파이프라인
    from refinery.pipeline import rederive_downstream as _rfs_rederive  # [r266] 편집 후 다운스트림 재조정
    from refinery.from_mindmap import branches_to_nodes as _rfs_mm_to_nodes  # [r253] 마인드맵→노드
    from refinery.context import build_project_context as _rfs_build_ctx  # [r253] 프로젝트 컨텍스트
    from refinery.wiki_structure import derive_wiki_structure as _rfs_derive_wiki  # [r256] 위키 표준분류 구조
    from refinery.rag import retrieve_relevant as _rfs_rag  # [r261] 자체 RAG(경량 키워드)
    from refinery.rag import retrieve_semantic as _rfs_rag_sem  # [r262] 임베딩 의미검색 RAG
except Exception as _rfs_imp_err:
    import traceback as _rfs_tb
    _REFINERY_OK = False
    _REFINERY_ERR = f"{type(_rfs_imp_err).__name__}: {_rfs_imp_err}"
    print("[main] ⚠ 연구 정련소 모듈 import 실패 — /refinery/* 비활성, 나머지는 정상")
    print(_rfs_tb.format_exc())
    # 더미 — 엔드포인트 정의 시점 NameError 방지 (호출 시 503)
    def _rfs_unavailable(*a, **k):
        raise HTTPException(503, f"연구 정련소 모듈 미로드: {_REFINERY_ERR}")
    class _RfsStub:
        SCHEMA_SQL = "-- refinery 모듈 미로드"
        def __getattr__(self, _n): return _rfs_unavailable
    _rfs = _RfsStub()
    require_author = _rfs_unavailable
    _rfs_decompose = _rfs_classify = _rfs_similar = _rfs_unavailable
    _rfs_build_tree = _rfs_compose_body = _rfs_compose_vault_list = _rfs_compose_changelog = _rfs_unavailable
    _rfs_link = _rfs_propose = _rfs_apply_tree = _rfs_apply_work = _rfs_unavailable
    _rfs_analyze = _rfs_unavailable
    _rfs_intake = _rfs_wiki_body = _rfs_unavailable
    _rfs_derive_stream = _rfs_apply_stream = _rfs_mm_to_nodes = _rfs_build_ctx = _rfs_unavailable
    _rfs_derive_wiki = _rfs_rederive = _rfs_unavailable
    def _rfs_rag(*a, **k):
        return ""
    async def _rfs_rag_sem(*a, **k):
        return ""


def _refinery_guard():
    """refinery 엔드포인트 진입 가드 — 미로드 시 503."""
    if not _REFINERY_OK:
        raise HTTPException(503, f"연구 정련소 모듈 미로드 (백엔드 재시작 필요): {_REFINERY_ERR}")


# [r242] 회의록 자동작성 모듈 — import 실패해도 백엔드 전체는 생존, /meetings/* 만 503.
_MEET_OK = True
_MEET_ERR = None
try:
    from meetings import store as _mtg_store
    from meetings import craig_client as _mtg_craig
    from meetings import transcribe as _mtg_tr
    from meetings import summarize as _mtg_sum
except Exception as _mtg_imp_err:
    import traceback as _mtg_tb
    _MEET_OK = False
    _MEET_ERR = f"{type(_mtg_imp_err).__name__}: {_mtg_imp_err}"
    print("[main] ⚠ 회의록 모듈 import 실패 — /meetings/* 비활성, 나머지 정상")
    print(_mtg_tb.format_exc())
    _mtg_store = _mtg_craig = _mtg_tr = _mtg_sum = None


def _meet_guard():
    if not _MEET_OK:
        raise HTTPException(503, f"회의록 모듈 미로드 (백엔드 재시작 필요): {_MEET_ERR}")


class RefinerySessionCreate(BaseModel):
    project_id: Optional[str] = None
    title: str
    user_id: str


class RefinerySessionPatch(BaseModel):
    user_id: str
    patch: Dict[str, Any]
    action: str = "session_updated"
    detail: str = ""


class RefineryDecomposeRequest(BaseModel):
    session_id: str
    project_id: Optional[str] = None
    user_id: str
    vault_docs: List[Dict[str, Any]] = []  # [{id, title, content}]
    model: Optional[str] = None


class RefineryClassifyRequest(BaseModel):
    session_id: str
    user_id: str
    nodes: List[Dict[str, Any]] = []
    model: Optional[str] = None
    batch_size: int = 5


class RefinerySimilarRequest(BaseModel):
    nodes: List[Dict[str, Any]]
    threshold: float = 0.85


class RefineryComposeRequest(BaseModel):
    session_id: str
    project_id: Optional[str] = None
    user_id: str
    nodes: List[Dict[str, Any]]
    classifications: Dict[str, str]  # {node_id: 'canon'|'hyp'|'later'|'cut'}
    vault_docs: List[Dict[str, Any]] = []
    session_title: str
    model: Optional[str] = None


class RefineryFileOverride(BaseModel):
    path: str
    body: str
    target_kind: str = "canon"
    visibility: str = "public"  # personal 일 때만
    category: Optional[str] = None
    node_ids: List[str] = []
    is_overview: bool = False


class RefineryApplyTreeRequest(BaseModel):
    session_id: str
    user_id: str
    project_id: Optional[str] = None
    files: List[RefineryFileOverride]  # 선택된 파일만
    create_archive: bool = True


class RefineryProposeWorkRequest(BaseModel):
    session_id: str
    user_id: str
    nodes: List[Dict[str, Any]]
    classifications: Dict[str, str]
    model: Optional[str] = None


class RefineryApplyWorkRequest(BaseModel):
    session_id: str
    user_id: str
    project_id: Optional[str] = None
    wbs_nodes: List[Dict[str, Any]] = []
    tasks: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    stages: List[Dict[str, Any]] = []    # [r247] product_stages
    sprints: List[Dict[str, Any]] = []   # [r247] 신규 스프린트


class RefineryIntakeRequest(BaseModel):
    """[r247] 들여오기 추천 — 분해 구조를 프로젝트로 들여오는 계획 제안(SSE)."""
    session_id: str
    user_id: str
    project_id: Optional[str] = None
    nodes: List[Dict[str, Any]] = []
    vault_docs: List[Dict[str, Any]] = []
    model: Optional[str] = None


class RefineryComposeWikiBodyRequest(BaseModel):
    """[r247] 채택된 위키 구조(목차) → 문서 본문 작성(SSE)."""
    session_id: str
    user_id: str
    project_id: Optional[str] = None
    docs: List[Dict[str, Any]] = []      # [{title, summary, outline, node_ids?}]
    nodes: List[Dict[str, Any]] = []
    vault_docs: List[Dict[str, Any]] = []
    model: Optional[str] = None


class RefineryDeriveStreamRequest(BaseModel):
    """[r253] 도출 파이프라인(B~E) — 키워드 노드 → Task/Sprint/STAGE/WBS(SSE)."""
    session_id: str
    user_id: str
    project_id: Optional[str] = None
    nodes: List[Dict[str, Any]] = []
    cross_links: List[Dict[str, Any]] = []
    pm_tax: List[Dict[str, Any]] = []          # 프론트 PM_TAX(공정태그)
    wiki_tax: List[Dict[str, Any]] = []        # [r263] 프론트 WIKI_TAX — 연속 위키 도출
    project_state: Dict[str, Any] = {}         # {stages,wbs,sprints,categories} — 컨텍스트
    capacity_hours: float = 80.0
    strict: bool = False
    rule: str = "auto"
    start_date: str = "2026-01-05"
    sprint_weeks: int = 2
    with_wiki: bool = True
    model: Optional[str] = None


class RefineryApplyStreamRequest(BaseModel):
    """[r253] 도출 Stream 채택분 → 실제 테이블 생성."""
    session_id: str
    user_id: str
    project_id: Optional[str] = None
    stream_id: Optional[str] = None
    stages: List[Dict[str, Any]] = []
    sprints: List[Dict[str, Any]] = []
    tasks: List[Dict[str, Any]] = []
    wbs: List[Dict[str, Any]] = []
    start_date: str = "2026-01-05"
    sprint_weeks: int = 2
    default_cat_id: Optional[str] = None


class RefineryDeriveWikiRequest(BaseModel):
    """[r256] 위키 표준분류(WIKI_TAX) 구조 도출(SSE)."""
    session_id: str
    user_id: str
    project_id: Optional[str] = None
    nodes: List[Dict[str, Any]] = []
    wiki_tax: List[Dict[str, Any]] = []
    project_state: Dict[str, Any] = {}
    model: Optional[str] = None


@app.get("/refinery/sessions")
async def refinery_list_sessions(project_id: Optional[str] = None):
    return {"sessions": _rfs.list_sessions(project_id)}


@app.get("/refinery/sessions/{sid}")
async def refinery_get_session(sid: str):
    s = _rfs.get_session(sid)
    if not s:
        raise HTTPException(404, f"세션 {sid} 없음")
    return s


@app.post("/refinery/sessions")
async def refinery_create_session(req: RefinerySessionCreate):
    require_author(req.user_id, "세션 생성")
    try:
        s = _rfs.create_session(project_id=req.project_id, title=req.title, user_id=req.user_id)
        return s
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.patch("/refinery/sessions/{sid}")
async def refinery_update_session(sid: str, req: RefinerySessionPatch):
    require_author(req.user_id, "세션 수정")
    try:
        return _rfs.update_session(sid, req.patch, user_id=req.user_id, action=req.action, detail=req.detail)
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.delete("/refinery/sessions/{sid}")
async def refinery_delete_session(sid: str, user_id: str):
    require_author(user_id, "세션 삭제")
    ok = _rfs.delete_session(sid, user_id=user_id)
    return {"deleted": ok}


@app.post("/refinery/analyze-structure")
async def refinery_analyze_structure(req: RefineryDecomposeRequest):
    """[r246] 로직 우선 분해 — LLM 없이 md 헤더 목차 + 섹션별 키워드 빈도로 즉시 구조화."""
    require_author(req.user_id, "구조 분석")
    if not _REFINERY_OK:
        raise HTTPException(503, f"연구 정련소 모듈 미로드: {_REFINERY_ERR}")
    result = _rfs_analyze(req.vault_docs)
    return result


@app.post("/refinery/decompose")
async def refinery_decompose(req: RefineryDecomposeRequest):
    """vault → 노드 트리 (SSE 분할 호출)."""
    require_author(req.user_id, "분해")
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업 중'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    gen = _rfs_decompose(
        session_id=req.session_id,
        project_id=req.project_id,
        vault_docs=req.vault_docs,
        model=req.model,
        user_id=req.user_id,
    )
    return _sse_indexer(gen, busy_kind="refinery_decompose", busy_project=req.project_id)


@app.post("/refinery/classify-suggest")
async def refinery_classify_suggest(req: RefineryClassifyRequest):
    require_author(req.user_id, "분류 추천")
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업 중'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    gen = _rfs_classify(nodes=req.nodes, model=req.model, batch_size=req.batch_size)
    return _sse_indexer(gen, busy_kind="refinery_classify")


@app.post("/refinery/similar-nodes")
async def refinery_similar_nodes(req: RefinerySimilarRequest):
    groups = await _rfs_similar(req.nodes, threshold=req.threshold)
    return {"groups": groups}


@app.post("/refinery/compose-tree")
async def refinery_compose_tree(req: RefineryComposeRequest):
    """트리 합성 + 파일 본문 자동 작성 (SSE)."""
    require_author(req.user_id, "정의서 작성")
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업 중'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")

    async def _gen():
        yield {"event": "stage", "message": "트리 골격 생성 중"}
        skeleton = _rfs_build_tree(
            session_title=req.session_title,
            nodes=req.nodes,
            classifications=req.classifications,
            project_id=req.project_id,
        )
        files = skeleton["files"]
        total = len(files)
        yield {"event": "tree_built", "root": skeleton["root"], "files_count": total}
        # session mock — composer 가 참고
        session = {"id": req.session_id, "title": req.session_title}
        composed_files: List[Dict[str, Any]] = []
        # 노드 인덱스
        node_by_id = {n["id"]: n for n in req.nodes}
        for idx, fmeta in enumerate(files):
            yield {"event": "file_start", "current": idx + 1, "total": total, "path": fmeta["path"]}
            if fmeta.get("is_vault_list"):
                body = _rfs_compose_vault_list(vault_docs=req.vault_docs, session=session, user_id=req.user_id)
            elif fmeta.get("is_changelog"):
                body = _rfs_compose_changelog(session=session, user_id=req.user_id)
            else:
                related = [f for f in files if f["path"] != fmeta["path"]]
                included = [node_by_id[nid] for nid in (fmeta.get("node_ids") or []) if nid in node_by_id]
                if not included and not fmeta.get("is_overview"):
                    # 노드 없는 일반 파일은 skip
                    continue
                body = await _rfs_compose_body(
                    file_meta=fmeta,
                    nodes=included,
                    related_files=related,
                    session=session,
                    user_id=req.user_id,
                    model=req.model,
                )
            composed_files.append({**fmeta, "body": body})
            yield {
                "event": "file_done", "current": idx + 1, "total": total,
                "path": fmeta["path"], "body_len": len(body),
                "percent": round((idx + 1) / total * 100, 1),
            }
        # 옵시디언 링크 후처리
        yield {"event": "stage", "message": "옵시디언 [[]] 링크 후처리"}
        linked = _rfs_link(composed_files)
        yield {"event": "tree_composed", "files": linked, "files_count": len(linked)}
        yield {"event": "done", "summary": {"files_count": len(linked)}}

    return _sse_indexer(_gen(), busy_kind="refinery_compose_tree", busy_project=req.project_id)


@app.post("/refinery/apply-tree")
async def refinery_apply_tree(req: RefineryApplyTreeRequest):
    """트리 일괄 commit (부분 선택은 프론트가 files 에 포함 여부로 결정)."""
    require_author(req.user_id, "위키 적용")
    s = _rfs.get_session(req.session_id)
    if not s:
        raise HTTPException(404, "세션 없음")
    result = _rfs_apply_tree(
        session=s,
        files=[f.model_dump() for f in req.files],
        user_id=req.user_id,
        project_id=req.project_id,
        create_archive=req.create_archive,
    )
    # 세션에 생성 결과 기록
    new_doc_ids = [c["id"] for c in result["created"]] + [u["id"] for u in result["updated"]]
    _rfs.update_session(
        req.session_id,
        {
            "status": "published",
            "generated_doc_ids": (s.get("generated_doc_ids") or []) + new_doc_ids,
        },
        user_id=req.user_id, action="wiki_applied",
        detail=f"신규 {len(result['created'])} · 갱신 {len(result['updated'])} · 경고 {len(result['warnings'])}",
    )
    return result


@app.post("/refinery/propose-work")
async def refinery_propose_work(req: RefineryProposeWorkRequest):
    """WBS/태스크/이슈 제안 (SSE)."""
    require_author(req.user_id, "작업 제안")
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업 중'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    gen = _rfs_propose(nodes=req.nodes, classifications=req.classifications, model=req.model)
    return _sse_indexer(gen, busy_kind="refinery_propose_work")


@app.post("/refinery/apply-work")
async def refinery_apply_work(req: RefineryApplyWorkRequest):
    """제안된 WBS/태스크/이슈 일괄 생성."""
    require_author(req.user_id, "작업 생성")
    s = _rfs.get_session(req.session_id)
    if not s:
        raise HTTPException(404, "세션 없음")
    result = _rfs_apply_work(
        session=s, wbs_nodes=req.wbs_nodes, tasks=req.tasks, issues=req.issues,
        user_id=req.user_id, project_id=req.project_id,
        stages=req.stages, sprints=req.sprints,  # [r247]
    )
    # [r249] STAGE/스프린트 id 는 전용 컬럼이 없으므로 허용된 generated_tree(jsonb)에 stash
    #   (신규 컬럼은 미마이그레이션 DB 에서 PostgREST 거부를 유발 → generated_tree 재사용이 안전).
    _gt = dict(s.get("generated_tree") or {})
    _gt["generated_stage_ids"] = (_gt.get("generated_stage_ids") or []) + [x["id"] for x in result.get("stages_created", [])]
    _gt["generated_sprint_ids"] = (_gt.get("generated_sprint_ids") or []) + [x["id"] for x in result.get("sprints_created", [])]
    _rfs.update_session(
        req.session_id,
        {
            "generated_wbs_ids": (s.get("generated_wbs_ids") or []) + [w["id"] for w in result["wbs_created"]],
            "generated_task_ids": (s.get("generated_task_ids") or []) + [t["id"] for t in result["tasks_created"]],
            "generated_issue_ids": (s.get("generated_issue_ids") or []) + [i["id"] for i in result["issues_created"]],
            "generated_tree": _gt,  # [r249] STAGE/스프린트 이력 포함
        },
        user_id=req.user_id, action="work_applied",
        detail=(
            f"STAGE {len(result.get('stages_created', []))} · 스프린트 {len(result.get('sprints_created', []))} · "
            f"WBS {len(result['wbs_created'])} · 태스크 {len(result['tasks_created'])} · 이슈 {len(result['issues_created'])}"
        ),
    )
    return result


@app.post("/refinery/intake-plan")
async def refinery_intake_plan(req: RefineryIntakeRequest):
    """[r247] 들여오기 추천 — 분해 구조 → STAGE/WBS+태스크/스프린트/이슈/위키구조 제안(SSE)."""
    require_author(req.user_id, "들여오기 추천")
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업 중'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    gen = _rfs_intake(nodes=req.nodes, vault_docs=req.vault_docs, model=req.model)
    return _sse_indexer(gen, busy_kind="refinery_intake")


@app.post("/refinery/compose-wiki-body")
async def refinery_compose_wiki_body(req: RefineryComposeWikiBodyRequest):
    """[r247] 채택된 위키 구조(목차) → 문서 본문 작성(SSE). files[] 는 이후 apply-tree 로 커밋."""
    require_author(req.user_id, "위키 본문 작성")
    s = _rfs.get_session(req.session_id)
    if not s:
        raise HTTPException(404, "세션 없음")
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업 중'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    gen = _rfs_wiki_body(
        docs=req.docs, nodes=req.nodes, vault_docs=req.vault_docs,
        session=s, user_id=req.user_id, model=req.model,
    )
    return _sse_indexer(gen, busy_kind="refinery_wiki_body")


@app.post("/refinery/derive-stream")
async def refinery_derive_stream(req: RefineryDeriveStreamRequest):
    """[r253] 도출 파이프라인 — 키워드 노드 → Task→Sprint→STAGE→WBS (SSE)."""
    require_author(req.user_id, "도출 파이프라인")
    if not _REFINERY_OK:
        raise HTTPException(503, f"연구 정련소 모듈 미로드: {_REFINERY_ERR}")
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업 중'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    ctx = _rfs_build_ctx({**(req.project_state or {}), "pm_tax": req.pm_tax})
    try:  # [r262] 자체 RAG — 의미검색(임베딩) 우선, 실패 시 키워드 폴백
        _rag = await _rfs_rag_sem(nodes=req.nodes, project_id=req.project_id)
        if not _rag:
            _rag = _rfs_rag(nodes=req.nodes, project_id=req.project_id)
        if _rag:
            ctx = ctx + "\n\n" + _rag
    except Exception:
        pass
    gen = _rfs_derive_stream(
        nodes=req.nodes, cross_links=req.cross_links, pm_tax=req.pm_tax, context=ctx,
        capacity_hours=req.capacity_hours, strict=req.strict, rule=req.rule,
        start_date=req.start_date, sprint_weeks=req.sprint_weeks,
        wiki_tax=req.wiki_tax, with_wiki=req.with_wiki, model=req.model,
    )
    return _sse_indexer(gen, busy_kind="refinery_derive_stream")


class RefineryRederiveRequest(BaseModel):
    """[r266] 사용자 편집(Task 추가/삭제) 후 다운스트림(Sprint/STAGE/WBS) 재조정(SSE)."""
    session_id: str
    user_id: str
    project_id: Optional[str] = None
    tasks: List[Dict[str, Any]] = []
    cross_links: List[Dict[str, Any]] = []
    capacity_hours: float = 80.0
    strict: bool = False
    rule: str = "auto"
    start_date: str = "2026-01-05"
    sprint_weeks: int = 2
    model: Optional[str] = None


@app.post("/refinery/rederive-downstream")
async def refinery_rederive_downstream(req: RefineryRederiveRequest):
    """[r266] Task 편집 반영 → 의존/Sprint/STAGE/WBS 재도출(SSE)."""
    require_author(req.user_id, "다운스트림 재조정")
    if not _REFINERY_OK:
        raise HTTPException(503, f"연구 정련소 모듈 미로드: {_REFINERY_ERR}")
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업 중'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    gen = _rfs_rederive(
        tasks=req.tasks, cross_links=req.cross_links,
        capacity_hours=req.capacity_hours, strict=req.strict, rule=req.rule,
        start_date=req.start_date, sprint_weeks=req.sprint_weeks, model=req.model,
    )
    return _sse_indexer(gen, busy_kind="refinery_rederive")


@app.post("/refinery/apply-stream")
async def refinery_apply_stream(req: RefineryApplyStreamRequest):
    """[r253] 도출 Stream 채택분 → 실제 STAGE/Sprint/Task/WBS 생성."""
    require_author(req.user_id, "Stream 생성")
    s = _rfs.get_session(req.session_id)
    if not s:
        raise HTTPException(404, "세션 없음")
    result = _rfs_apply_stream(
        session=s, stages=req.stages, sprints=req.sprints, tasks=req.tasks, wbs=req.wbs,
        user_id=req.user_id, project_id=req.project_id, stream_id=req.stream_id,
        start_date=req.start_date, sprint_weeks=req.sprint_weeks, default_cat_id=req.default_cat_id,
    )
    # 세션 이력에 생성 id 누적(generated_tree 에 stash — r249 패턴)
    _gt = dict(s.get("generated_tree") or {})
    _gt["stream_id"] = result.get("stream_id")
    _gt["generated_stage_ids"] = (_gt.get("generated_stage_ids") or []) + [x["id"] for x in result.get("stages_created", [])]
    _gt["generated_sprint_ids"] = (_gt.get("generated_sprint_ids") or []) + [x["id"] for x in result.get("sprints_created", [])]
    try:
        _rfs.update_session(
            req.session_id,
            {
                "generated_wbs_ids": (s.get("generated_wbs_ids") or []) + [w["id"] for w in result.get("wbs_created", [])],
                "generated_task_ids": (s.get("generated_task_ids") or []) + [t["id"] for t in result.get("tasks_created", [])],
                "generated_tree": _gt,
            },
            user_id=req.user_id, action="stream_applied",
            detail=(
                f"STAGE {len(result.get('stages_created', []))} · Sprint {len(result.get('sprints_created', []))} · "
                f"Task {len(result.get('tasks_created', []))} · WBS {len(result.get('wbs_created', []))}"
            ),
        )
    except Exception as e:
        result.setdefault("warnings", []).append(f"세션 이력 갱신 실패(생성은 완료): {e}")
    return result


@app.post("/refinery/decompose-mindmap")
async def refinery_decompose_mindmap(req: RefineryDecomposeRequest):
    """[r255] ③ 분해 — 마인드맵 고도화 추출기 재사용(결정 #1). 트리+cross_links → 정련소 노드."""
    require_author(req.user_id, "마인드맵 분해")
    if not _REFINERY_OK:
        raise HTTPException(503, f"연구 정련소 모듈 미로드: {_REFINERY_ERR}")
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업 중'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    docs = [{"id": d.get("id"), "title": d.get("title"), "content": d.get("content"), "attachment_urls": d.get("attachment_urls") or []} for d in (req.vault_docs or [])]

    async def gen():
        diagram = None
        central = ""
        branches = []
        async for ev in generate_mindmap(docs=docs, model=req.model, mode="auto"):
            if ev.get("event") == "done":
                diagram = ev.get("diagram") or {}
                central = ev.get("central") or "마인드맵"
                branches = ev.get("branches_raw") or []
                yield {"event": "stage", "message": "마인드맵 추출 완료 → 정련소 노드 변환"}
            elif ev.get("event") in ("stage", "progress", "warn", "error"):
                yield ev
        # cross_links: diagram 의 dashed 엣지(개념 관계) → 제목쌍
        node_title = {}
        for nd in (diagram or {}).get("nodes", []):
            node_title[nd.get("id")] = nd.get("title")
        cls = []
        for e in (diagram or {}).get("edges", []):
            if e.get("style") == "dashed":
                a, b = node_title.get(e.get("from")), node_title.get(e.get("to"))
                if a and b:
                    cls.append({"from": a, "to": b, "label": e.get("label") or ""})
        conv = _rfs_mm_to_nodes(central=central, branches=branches, cross_links=cls)
        yield {
            "event": "done",
            "nodes": conv["nodes"], "cross_links": conv["cross_links"],
            "node_count": len(conv["nodes"]),
            "leaf_count": sum(1 for n in conv["nodes"] if n.get("kind") != "category"),
            "cross_link_count": len(conv["cross_links"]),
            "summary": f"노드 {len(conv['nodes'])} · 개념관계 {len(conv['cross_links'])}",
        }
    return _sse_indexer(gen(), busy_kind="refinery_decompose_mindmap")


@app.post("/refinery/derive-wiki")
async def refinery_derive_wiki(req: RefineryDeriveWikiRequest):
    """[r256] ⑤ 위키 구조 — 분해 노드 → WIKI_TAX 표준분류 매핑 문서 구조(목차) SSE."""
    require_author(req.user_id, "위키 구조 도출")
    if not _REFINERY_OK:
        raise HTTPException(503, f"연구 정련소 모듈 미로드: {_REFINERY_ERR}")
    if _busy_active():
        async def busy_gen():
            yield f"data: {json.dumps({'event': 'error', 'message': '다른 LLM 작업 중'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(busy_gen(), media_type="text/event-stream")
    ctx = _rfs_build_ctx(req.project_state or {})
    try:  # [r262] 자체 RAG — 의미검색 우선, 키워드 폴백 (중복 회피·맥락 보강)
        _rag = await _rfs_rag_sem(nodes=req.nodes, project_id=req.project_id)
        if not _rag:
            _rag = _rfs_rag(nodes=req.nodes, project_id=req.project_id)
        if _rag:
            ctx = ctx + "\n\n" + _rag
    except Exception:
        pass
    gen = _rfs_derive_wiki(nodes=req.nodes, wiki_tax=req.wiki_tax, context=ctx, model=req.model)
    return _sse_indexer(gen, busy_kind="refinery_derive_wiki")


@app.get("/refinery/schema-sql")
async def refinery_schema_sql():
    """설정→DB 스키마 점검 SQL 에 포함될 SQL 반환."""
    return {"sql": _rfs.SCHEMA_SQL}


# ════════════════════════════════════════════════════════════
# [r242] 회의록 자동작성 — Craig(Discord 음성) → faster-whisper STT → LLM 회의록
# ════════════════════════════════════════════════════════════
class MeetingImportRequest(BaseModel):
    user_id: str
    project_id: Optional[str] = None
    title: str = ""
    craig_id: Optional[str] = None      # 녹음 ID 또는 rec 링크
    craig_url: Optional[str] = None     # 직접 다운로드 URL(폴백)
    upload_token: Optional[str] = None  # [r243] 파일 업로드 폴백(/meetings/upload 가 준 토큰)
    key: Optional[str] = None
    whisper_model: str = "medium"       # tiny|base|small|medium|large-v3
    language: str = "ko"
    model: Optional[str] = None         # 요약 LLM(미지정=기본)


class MeetingSummarizeRequest(BaseModel):
    model: Optional[str] = None


class MeetingExportRequest(BaseModel):
    user_id: str
    project_id: Optional[str] = None


@app.get("/meetings/health")
async def meetings_health():
    av = _mtg_tr.is_available() if _MEET_OK else {"faster_whisper": False, "ffmpeg": False, "error": _MEET_ERR}
    return {"ok": _MEET_OK, "revision": SERVER_REVISION, "error": _MEET_ERR, "stt": av}


@app.get("/meetings/schema-sql")
async def meetings_schema_sql():
    return {"sql": (_mtg_store.SCHEMA_SQL if _MEET_OK else "-- 회의록 모듈 미로드")}


@app.post("/meetings/upload")
async def meetings_upload(files: List[UploadFile] = File(...)):
    """[r243] Craig multi-track zip(또는 오디오 파일)을 업로드 → import 에서 쓸 토큰 반환.

    Craig 자동 다운로드가 안 될 때의 확실한 폴백. zip 은 자동 압축해제.
    """
    _meet_guard()
    import os as _os, tempfile as _tmp, time as _t, zipfile as _zip
    token = f"up_{int(_t.time() * 1000)}_{int.from_bytes(_os.urandom(2), 'big')}"
    updir = _os.path.join(_tmp.gettempdir(), "mtg_uploads", token)
    _os.makedirs(updir, exist_ok=True)
    saved = []
    for f in files:
        name = _os.path.basename(f.filename or "track")
        if not name:
            continue
        dst = _os.path.join(updir, name)
        with open(dst, "wb") as out:
            while True:
                chunk = await f.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        saved.append(name)
        if name.lower().endswith(".zip"):
            try:
                with _zip.ZipFile(dst) as z:
                    z.extractall(updir)
            except Exception:
                pass
    return {"upload_token": token, "files": saved}


@app.get("/meetings/sessions")
async def meetings_list(project_id: Optional[str] = None):
    _meet_guard()
    return {"sessions": _mtg_store.list_sessions(project_id)}


@app.get("/meetings/sessions/{sid}")
async def meetings_get(sid: str):
    _meet_guard()
    s = _mtg_store.get_session(sid)
    if not s:
        raise HTTPException(404, "회의 세션 없음")
    return s


@app.delete("/meetings/sessions/{sid}")
async def meetings_delete(sid: str):
    _meet_guard()
    return {"ok": _mtg_store.delete_session(sid)}


# [r244] 백그라운드 회의 처리 작업 추적(GC 방지 + 동시 1건 제한)
_MEET_TASKS: set = set()        # 처리 중 session id
_MEET_TASK_REFS: set = set()    # asyncio.Task 참조 보존


async def _meet_run_job(p: dict, sid: str):
    """백그라운드: 다운로드/업로드 → 화자별 STT(증분 저장) → 요약 → ready.

    SSE 와 무관하게 독립 실행되어 클라이언트가 창을 닫아도 끝까지 진행된다.
    진행상황은 meeting_sessions.status + segments 증분으로 기록(프론트가 폴링).
    """
    import os as _os, tempfile as _tmp, shutil as _sh
    loop = asyncio.get_event_loop()
    upload_token = p.get("upload_token")
    if upload_token:
        workdir = _os.path.join(_tmp.gettempdir(), "mtg_uploads", upload_token)
    else:
        workdir = _tmp.mkdtemp(prefix="mtg_")

    async def _upd(patch):
        await loop.run_in_executor(None, lambda: _mtg_store.update_session(sid, patch))

    try:
        _busy_set(running=True, kind="meeting_import", started_at=time.time(), stage="prepare", message="회의 처리 시작")
        # 1) 오디오 확보
        if upload_token:
            if not _os.path.isdir(workdir):
                await _upd({"status": "error", "summary": {"error": "업로드 파일을 찾지 못함(토큰 만료). 다시 업로드하세요."}})
                return
        else:
            await _upd({"status": "importing"})
            _busy_set(running=True, kind="meeting_import", stage="download", message="Craig 녹음 다운로드")
            if p.get("craig_url"):
                await loop.run_in_executor(None, lambda: _mtg_craig.download_url(p["craig_url"], workdir))
            else:
                await loop.run_in_executor(None, lambda: _mtg_craig.download_recording(p.get("craig_id"), p.get("key"), workdir))
        # 2) 트랙
        tracks = await loop.run_in_executor(None, lambda: _mtg_tr.list_tracks(workdir))
        if not tracks:
            await _upd({"status": "error", "summary": {"error": "오디오 트랙을 찾지 못함(다운로드/업로드 형식 확인)"}})
            return
        await _upd({"status": "transcribing"})
        # 3) 화자별 STT(증분 저장 — 폴링이 자라는 대화내역을 보여줌)
        all_segs, participants = [], []
        for i, tk in enumerate(tracks):
            _busy_set(running=True, kind="meeting_import", stage="stt", current=i + 1, total=len(tracks),
                      category=tk["speaker"], message=f"STT {i + 1}/{len(tracks)} — {tk['speaker']}")
            segs = await loop.run_in_executor(
                None, lambda pa=tk["path"], sp=tk["speaker"]: _mtg_tr.transcribe_track(
                    pa, sp, p.get("whisper_model", "medium"), p.get("language", "ko")))
            all_segs.extend(segs)
            participants.append({"name": tk["speaker"], "track": _os.path.basename(tk["path"])})
            merged = _mtg_tr.merge_segments(all_segs)
            await _upd({"status": "transcribing", "segments": merged,
                        "transcript_text": _mtg_tr.segments_to_text(merged), "participants": participants})
        merged = _mtg_tr.merge_segments(all_segs)
        dur = int(max([s["t"] + s["dur"] for s in merged], default=0))
        await _upd({"status": "summarizing", "segments": merged, "transcript_text": _mtg_tr.segments_to_text(merged),
                    "participants": participants, "duration_sec": dur, "model": p.get("whisper_model")})
        # 4) 요약
        _busy_set(running=True, kind="meeting_import", stage="summarize", message="회의록 요약(LLM)")
        summary = await _mtg_sum.summarize(merged, title=p.get("title", ""), model=p.get("model"))
        await _upd({"status": "ready", "summary": summary})
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        try:
            await _upd({"status": "error", "summary": {"error": str(e)[:400]}})
        except Exception:
            pass
    finally:
        _busy_clear()
        try:
            _sh.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass
        _MEET_TASKS.discard(sid)


@app.post("/meetings/import")
async def meetings_import(req: MeetingImportRequest):
    """Craig 녹음/업로드 → STT → 회의록 요약을 **백그라운드**로 시작. 즉시 {id} 반환.

    실제 처리는 _meet_run_job 이 독립적으로 수행 → 창을 닫아도 끝까지 진행.
    진행상황은 GET /meetings/sessions/{id} 폴링으로 확인.
    """
    _meet_guard()
    if not (req.user_id or "").strip():
        raise HTTPException(422, "user_id(작성자) 필요")
    if not (req.craig_id or req.craig_url or req.upload_token):
        raise HTTPException(422, "녹음 ID/URL 또는 업로드 파일이 필요합니다")
    av = _mtg_tr.is_available()
    if not av.get("faster_whisper") or not av.get("ffmpeg"):
        raise HTTPException(400, "STT 준비 안 됨 — " + (av.get("error") or "faster-whisper/ffmpeg 설치 필요"))
    if _MEET_TASKS or _busy_active():
        raise HTTPException(409, "이미 처리 중인 작업이 있습니다 — 끝난 뒤 다시 시도하세요.")
    sess = _mtg_store.create_session(project_id=req.project_id, title=req.title or "회의",
                                     user_id=req.user_id, craig_id=req.craig_id)
    sid = sess["id"]
    _MEET_TASKS.add(sid)
    task = asyncio.create_task(_meet_run_job(req.model_dump(), sid))
    _MEET_TASK_REFS.add(task)
    task.add_done_callback(lambda t: _MEET_TASK_REFS.discard(t))
    return {"id": sid, "status": "importing"}


@app.post("/meetings/sessions/{sid}/summarize")
async def meetings_resummarize(sid: str, req: MeetingSummarizeRequest):
    _meet_guard()
    s = _mtg_store.get_session(sid)
    if not s:
        raise HTTPException(404, "회의 세션 없음")
    summary = await _mtg_sum.summarize(s.get("segments") or [], title=s.get("title") or "", model=req.model)
    _mtg_store.update_session(sid, {"summary": summary, "status": "ready"})
    return {"ok": True, "summary": summary}


@app.post("/meetings/sessions/{sid}/export-wiki")
async def meetings_export_wiki(sid: str, req: MeetingExportRequest):
    """회의록 요약을 wiki_docs 문서로 내보내기."""
    _meet_guard()
    s = _mtg_store.get_session(sid)
    if not s:
        raise HTTPException(404, "회의 세션 없음")
    md = _mtg_sum.to_markdown(s.get("title") or "회의", s.get("summary") or {},
                              started_at=s.get("started_at") or "", participants=s.get("participants") or [])
    import time as _t
    doc_id = f"doc_{int(_t.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"
    now_ms = int(_t.time() * 1000)
    row = {
        "id": doc_id, "project_id": req.project_id, "parent_id": None,
        "title": (s.get("title") or "회의") + " 회의록", "kind": "wiki", "emoji": "📝",
        "content": md, "meta": {"meetingSessionId": sid, "tags": ["meeting", "회의록"]},
        "sort_order": now_ms % 100000, "is_collapsed": False, "is_deprecated": False,
        "doc_type": "plan", "linked_task_ids": [], "linked_doc_ids": [], "is_locked": False,
        "created_by": req.user_id, "updated_by": req.user_id,
        "update_history": [{"action": "meeting_export", "by": req.user_id, "detail": "회의록 자동 내보내기"}],
        "updated_at": now_ms,
    }
    try:
        get_store().client.table("wiki_docs").insert(row).execute()
    except Exception as e:
        raise HTTPException(500, f"위키 내보내기 실패: {e}")
    _mtg_store.update_session(sid, {"wiki_doc_id": doc_id})
    return {"ok": True, "doc_id": doc_id}


# [r223] diff / promote
from refinery.diff_ops import session_diff as _rfs_diff, promote_hypothesis as _rfs_promote


class RefineryDiffRequest(BaseModel):
    session_id_a: str
    session_id_b: str


class RefineryPromoteRequest(BaseModel):
    session_id: str
    user_id: str
    node_ids: List[str]
    playtest_result: str = "positive"


@app.post("/refinery/diff")
async def refinery_diff(req: RefineryDiffRequest):
    a = _rfs.get_session(req.session_id_a)
    b = _rfs.get_session(req.session_id_b)
    if not a or not b: raise HTTPException(404, "세션 없음")
    return _rfs_diff(a, b)


@app.post("/refinery/promote")
async def refinery_promote(req: RefineryPromoteRequest):
    s = _rfs.get_session(req.session_id)
    if not s: raise HTTPException(404, "세션 없음")
    result = _rfs_promote(session=s, node_ids=req.node_ids, user_id=req.user_id, playtest_result=req.playtest_result)
    _rfs.update_session(req.session_id, result["session_patch"], user_id=req.user_id, action="hypothesis_promoted",
                       detail=f"{result['promoted_count']}개 → canon")
    return result


class AuthorMigrateRequest(BaseModel):
    user_id: str  # 요청자
    table: str = "wiki_docs"
    fill_with: str = "system:unknown"  # 이 값으로 NULL 채움
    dry_run: bool = True


@app.post("/refinery/admin/migrate-authors")
async def refinery_migrate_authors(req: AuthorMigrateRequest):
    """작성자 NULL 인 행 카운트 + (dry_run=false 시) 채움."""
    require_author(req.user_id, "마이그레이션")
    store = get_store()
    try:
        # NULL 카운트
        r = store.client.table(req.table).select("id", count="exact").is_("created_by", "null").limit(1).execute()
        null_count = r.count or 0
        if req.dry_run:
            return {"table": req.table, "null_count": null_count, "dry_run": True, "applied": 0}
        # 채움
        update_res = store.client.table(req.table).update({"created_by": req.fill_with}).is_("created_by", "null").execute()
        return {"table": req.table, "null_count_before": null_count, "applied": len(update_res.data or []), "fill_with": req.fill_with}
    except Exception as e:
        raise HTTPException(500, f"마이그레이션 실패: {e}")


class ForceClearRequest(BaseModel):
    confirm: bool = True


@app.post("/llm/force-clear")
async def llm_force_clear(req: ForceClearRequest = ForceClearRequest()):
    """[r225] LLM busy 강제 해제 — stuck 상태에서 사용자가 즉시 풀 수 있게."""
    prev = dict(LLM_BUSY_STATE)
    _busy_clear()
    return {"cleared": True, "previous_kind": prev.get("kind"), "was_running": prev.get("running")}


@app.get("/llm/busy")
async def llm_busy_status():
    """[r225] 현재 LLM busy 상태 — 정련소 진입 시 확인 (stale 자동 해제)."""
    active = _busy_active()
    return {
        "active": active,
        "kind": LLM_BUSY_STATE.get("kind") if active else None,
        "human": _busy_human() if active else None,
        "started_at": LLM_BUSY_STATE.get("started_at") if active else None,
        "stage": LLM_BUSY_STATE.get("stage") if active else None,
    }


# [r226] Gemini API 키 등록/조회/테스트
class GeminiConfigRequest(BaseModel):
    api_key: str
    label: Optional[str] = "내 Gemini"


@app.post("/llm/gemini-config")
async def llm_gemini_config(req: GeminiConfigRequest):
    """Gemini API 키 등록 (프로세스 전역). 키는 마스킹해서만 응답."""
    if not _HAS_GEMINI:
        raise HTTPException(503, "Gemini 모듈 미설치 (httpx 필요)")
    key = (req.api_key or "").strip()
    if not key:
        raise HTTPException(422, "api_key 필요")
    # 검증 — ping
    try:
        client = get_gemini(key)
        ok = await client.ping()
    except Exception as e:
        raise HTTPException(400, f"Gemini 키 검증 실패: {e}")
    if not ok:
        raise HTTPException(400, "Gemini 키가 유효하지 않습니다 (모델 목록 조회 실패)")
    GEMINI_CONFIG["api_key"] = key
    GEMINI_CONFIG["label"] = req.label or "내 Gemini"
    models = await client.list_models()
    free = [m for m in models if any(m.startswith(f) for f in GEMINI_FREE_MODELS)] or GEMINI_FREE_MODELS
    return {"ok": True, "label": GEMINI_CONFIG["label"], "masked": "..." + key[-4:], "free_models": free}


@app.get("/llm/gemini-config")
async def llm_gemini_config_get():
    """Gemini 키 등록 여부 (키 자체는 미노출)."""
    key = GEMINI_CONFIG.get("api_key")
    return {
        "configured": bool(key),
        "label": GEMINI_CONFIG.get("label"),
        "masked": ("..." + key[-4:]) if key else None,
        "has_module": _HAS_GEMINI,
        "free_models": GEMINI_FREE_MODELS,
    }


@app.delete("/llm/gemini-config")
async def llm_gemini_config_delete():
    GEMINI_CONFIG["api_key"] = None
    GEMINI_CONFIG["label"] = None
    return {"ok": True}


@app.get("/refinery/health")
async def refinery_health():
    """정련소 모듈 헬스 + /goal 워크플로 명세. (import 가드와 무관 — 항상 응답)"""
    return {
        "ok": _REFINERY_OK,
        "module_loaded": _REFINERY_OK,
        "module_error": _REFINERY_ERR,
        "revision": SERVER_REVISION,
        "modules": ["decomposer", "classifier", "similar_nodes", "composer", "linker", "work_proposer", "apply_ops", "diff_ops"],
        "goal_steps": [
            {"n": 1, "label": "세션 생성", "endpoint": "POST /refinery/sessions"},
            {"n": 2, "label": "vault import", "endpoint": "PATCH /refinery/sessions/{id}"},
            {"n": 3, "label": "분해", "endpoint": "POST /refinery/decompose"},
            {"n": 4, "label": "AI 분류 추천", "endpoint": "POST /refinery/classify-suggest"},
            {"n": 5, "label": "유사 노드 그룹", "endpoint": "POST /refinery/similar-nodes"},
            {"n": 6, "label": "분류 확정", "endpoint": "PATCH /refinery/sessions/{id}"},
            {"n": 7, "label": "결재 발행", "endpoint": "(외부) dbUpsertReview"},
            {"n": 8, "label": "결재 통과 대기", "endpoint": "(외부)"},
            {"n": 9, "label": "트리 합성", "endpoint": "POST /refinery/compose-tree"},
            {"n": 10, "label": "위키 적용", "endpoint": "POST /refinery/apply-tree"},
            {"n": 11, "label": "작업 제안", "endpoint": "POST /refinery/propose-work"},
            {"n": 12, "label": "작업 생성", "endpoint": "POST /refinery/apply-work"},
            {"n": 13, "label": "diff", "endpoint": "POST /refinery/diff"},
            {"n": 14, "label": "promote", "endpoint": "POST /refinery/promote"},
            {"n": 15, "label": "세션 archive", "endpoint": "PATCH /refinery/sessions/{id}"},
            {"n": 16, "label": "검증 완료", "endpoint": "(클라이언트)"},
        ],
    }


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
    if _busy_active():
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
    if _busy_active():
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
