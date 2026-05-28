"""[r108] Agentic Tool-Use 챗 루프.

기존 단순 RAG (rag.chat_stream) 를 대체. LLM이 능동적으로 도구를 호출.

흐름:
  사용자 질문 →
  ① LLM(tools 첨부) → tool_calls 결정 또는 직접 답변
  ② tool_calls 있으면 서버가 실행 → 결과를 새 message로 첨부
  ③ LLM 재호출 → 또 tool_calls 있으면 반복(최대 5회) 또는 최종 답변
  ④ 최종 답변은 스트리밍

기존 RAG 한계 해결:
- "진행중인 스프린트" → get_active_sprint 도구 직접 호출 → 실제 DB 데이터로 답
- "칸반 데이터 모델" → search_vector 도구 → 기존 벡터 검색 활용
"""
import json
from typing import AsyncIterator, Dict, Any, List, Optional
from ollama_client import get_ollama
from tools import TOOL_DEFINITIONS, execute_tool


SYSTEM_PROMPT = """당신은 TDA Dashboard 프로젝트의 능동적 어시스턴트입니다.

당신이 가진 도구:
- get_active_sprint(project_id): 현재 진행중(active) 스프린트의 메타+카드+카테고리 직접 조회
- list_tasks(project_id, sprint_id?, zone?, status?, due_before_days?, limit?): 칸반 카드 필터 검색
- search_vector(query, source_types?, top_k?): 코드/문서 의미 검색 (벡터)

원칙:
1. **능동 호출** — 사용자 질문을 분석해 필요한 도구를 직접 호출. 라이브 상태(진행중, 마감, 카드 목록 등)는 get_active_sprint/list_tasks. 코드·문서 설명은 search_vector.
2. **순차 추론** — 한 도구 결과만으로 부족하면 다른 도구도 호출. 예: 활성 스프린트 조회 → 그 스프린트의 카드 코멘트는 list_tasks로 다시.
3. **컨텍스트만 사용** — 도구가 반환한 JSON 데이터, search_vector가 반환한 청크 본문만 인용. 일반 상식·LLM 사전지식 금지.
4. **솔직** — 도구 결과가 빈 배열이면 "현재 인덱스/DB에 해당 데이터가 없습니다"라고 알리고, 사용자가 무엇을 만들면 좋을지 제안.
5. **한국어** — 답변은 한국어. 식별자·코드는 원문 유지.
6. **출처** — 답변에 사용한 데이터의 출처(스프린트 ID, 카드 제목, 파일 경로)를 [N] 형태로 인용.

금지:
- "일반적으로 ~는 ~합니다" 같은 일반 상식 답변
- 컨텍스트에 없는 가상 SQL·코드 예시
- 일반 칸반 용어("To Do", "In Progress") — 이 프로젝트는 Now/Shelf/Buried
"""


MAX_TOOL_ROUNDS = 5  # 무한 루프 방지


async def run(
    messages: List[Dict[str, str]],
    project_id: Optional[str] = None,
    model: Optional[str] = None,
    include_tasks: bool = True,  # 호환 — 안 씀
) -> AsyncIterator[Dict[str, Any]]:
    """기존 rag.chat_stream 시그니처와 호환. yield event 형식 동일.

    yields:
      {"meta": {...}}   - 도구 호출 진단 정보 (검색 칩용)
      {"delta": "..."}  - 최종 답변 토큰
      {"sources": [...]} - 참조 출처 (검색 칩 아래용)
    """
    if not messages:
        yield {"delta": "❌ 메시지가 비어있습니다."}
        return
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        yield {"delta": "❌ 질문이 없습니다."}
        return
    user_query = user_msgs[-1]["content"]

    # [r108] 시스템 프롬프트 + 사용자 질문 (project_id 자동 주입 컨텍스트)
    project_hint = f"\n\n[컨텍스트] project_id = \"{project_id}\" (도구 호출 시 사용)" if project_id else ""
    llm_messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT + project_hint},
    ]
    # 사용자 대화 히스토리 (최근 6개)
    for m in messages[-6:]:
        llm_messages.append({"role": m["role"], "content": m["content"]})

    ollama = get_ollama()
    tool_log: List[Dict[str, Any]] = []  # 도구 호출 기록 (UI 표시용)
    sources_collected: List[Dict[str, Any]] = []  # 검색 칩 아래 참조

    # ───── 도구 호출 루프 ─────
    for round_idx in range(MAX_TOOL_ROUNDS):
        try:
            assistant_msg = await ollama.chat_with_tools(
                messages=llm_messages,
                tools=TOOL_DEFINITIONS,
                model=model,
                temperature=0.2,
            )
        except Exception as e:
            yield {"delta": f"❌ LLM 호출 실패: {type(e).__name__}: {e}\n\nOllama·모델 상태 확인 필요."}
            return

        tool_calls = assistant_msg.get("tool_calls") or []
        if not tool_calls:
            # LLM이 도구 더 안 부름 → 이게 최종 답변 시도
            final_text = assistant_msg.get("content") or ""
            # 도구 호출 기록을 meta로 송신
            yield {"meta": _build_meta(tool_log, user_query)}
            # 최종 답변을 스트리밍 형태로 잘라서 송신 (UX 일관성)
            for chunk_text in _stream_chunks(final_text):
                yield {"delta": chunk_text}
            if sources_collected:
                yield {"sources": sources_collected}
            return

        # 도구 호출 메시지를 assistant로 추가
        llm_messages.append({
            "role": "assistant",
            "content": assistant_msg.get("content") or "",
            "tool_calls": tool_calls,
        })

        # 각 도구 호출 실행
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            # project_id 자동 주입 (LLM이 빠뜨려도 보정)
            if project_id and "project_id" in (fn.get("parameters", {}).get("properties", {}) or {}):
                args.setdefault("project_id", project_id)
            if name in ("get_active_sprint", "list_tasks") and project_id:
                args.setdefault("project_id", project_id)

            result = await execute_tool(name, args)
            tool_log.append({
                "tool": name,
                "args": args,
                "ok": result.get("ok", False),
                "summary": _short_result_summary(name, result),
            })
            # 도구 결과를 LLM에게 tool 메시지로 첨부
            llm_messages.append({
                "role": "tool",
                "content": json.dumps(result, ensure_ascii=False)[:6000],  # 길이 제한
            })
            # 사이드바 sources 누적 (search_vector / get_active_sprint / list_tasks)
            _collect_sources(name, result, sources_collected)

    # 라운드 한계 도달 — 마지막 시도로 최종 답변 강제
    llm_messages.append({
        "role": "user",
        "content": "도구 호출은 충분합니다. 위 결과를 종합해 한국어로 최종 답변을 작성하세요. 추가 도구 호출 금지.",
    })
    try:
        assistant_msg = await ollama.chat_with_tools(
            messages=llm_messages,
            tools=[],  # 도구 없이 답변만
            model=model,
            temperature=0.2,
        )
        final_text = assistant_msg.get("content") or "(응답 생성 실패)"
    except Exception as e:
        final_text = f"❌ 최종 답변 생성 실패: {e}"
    yield {"meta": _build_meta(tool_log, user_query)}
    for chunk_text in _stream_chunks(final_text):
        yield {"delta": chunk_text}
    if sources_collected:
        yield {"sources": sources_collected}


# ─────────────────────────────────────────────
# 헬퍼들
# ─────────────────────────────────────────────

def _stream_chunks(text: str, chunk_size: int = 40):
    """비스트리밍 응답을 스트리밍 흉내내기 — UX 일관성."""
    if not text:
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


def _short_result_summary(name: str, result: Dict[str, Any]) -> str:
    """검색 칩 hover 등에 쓸 짧은 요약."""
    if not result.get("ok"):
        return f"실패: {result.get('error', '?')[:60]}"
    r = result.get("result")
    if name == "get_active_sprint":
        if not r:
            return "활성 스프린트 없음"
        return f"{r.get('weekLabel', '?')} · 카드 {r.get('cardCount', 0)}개"
    if name == "list_tasks":
        return f"카드 {result.get('count', 0)}개"
    if name == "search_vector":
        return f"청크 {result.get('count', 0)}개"
    return "OK"


def _build_meta(tool_log: List[Dict[str, Any]], query: str) -> Dict[str, Any]:
    """검색 칩용 메타 — 도구 호출 사용 시 호환 정보."""
    # 기존 r94 메타 형식과 호환 + 도구 호출 정보 추가
    return {
        "retrieved": sum(1 for t in tool_log if t.get("ok")),
        "avg_sim": 0.0,
        "max_sim": 0.0,
        "min_sim": 0.0,
        "top": [
            {"type": "tool", "title": f"{t['tool']}({t['summary']})", "sim": 1.0}
            for t in tool_log[:5]
        ],
        "tool_calls": tool_log,
        "query": query,
    }


def _collect_sources(name: str, result: Dict[str, Any], out: List[Dict[str, Any]]):
    """도구 결과에서 사용자에게 보여줄 출처 추출 (UI 사이드바용)."""
    if not result.get("ok"):
        return
    r = result.get("result")
    if name == "get_active_sprint" and r:
        out.append({
            "type": "sprint",
            "id": r.get("id", ""),
            "title": r.get("weekLabel") or "active sprint",
        })
        for card in (r.get("cards") or [])[:3]:
            out.append({"type": "task", "id": card.get("id", ""), "title": card.get("title") or "(제목 없음)"})
    elif name == "list_tasks" and r:
        for card in r[:5]:
            out.append({"type": "task", "id": card.get("id", ""), "title": card.get("title") or "(제목 없음)"})
    elif name == "search_vector" and r:
        for chunk in r[:5]:
            st = chunk.get("source_type", "?")
            ftype = "doc" if st in ("code", "wiki") else "task"
            out.append({
                "type": ftype,
                "id": chunk.get("source_id", ""),
                "title": chunk.get("title") or chunk.get("source_id", "?"),
            })
