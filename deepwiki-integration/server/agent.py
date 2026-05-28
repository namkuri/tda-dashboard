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

## 사용 가능한 도구 (선택 가이드)

라이브 데이터(Supabase DB 직접 조회):
- **get_active_sprint(project_id)**: 진행중(active) 스프린트의 메타+카드+카테고리 — "이번/진행중 스프린트"
- **list_sprints(project_id, status?)**: 스프린트 목록 (메타만) — "지난 스프린트", "스프린트 히스토리"
- **list_tasks(project_id, sprint_id?, zone?, status?, due_before_days?, limit?)**: 칸반 카드 필터 — "내 카드", "마감 임박", "완료 카드"
- **list_docs(project_id, kind?, recent_first?, limit?)**: 위키/문서 목록 — "문서 목록", "프로젝트 위키", "최근 작성된 문서"
- **list_reviews(project_id, status?)**: 결재 요청 — "결재함", "내 결재 대기"
- **list_calendar_events(project_id?, from_date?, to_date?)**: 일정 — "이번달 일정", "오늘 미팅"
- **list_issues(project_id, status?, priority?)**: 이슈 트래커 — "이슈", "버그"
- **list_users(project_id?)**: 팀원 — "참여자", "팀원 누구"
- **get_project_info(project_id)**: 프로젝트 종합 메타 — 이름·카테고리·Git URL·참여자·전체 통계(docs/tasks/sprints/issues/reviews 개수). 사용자가 "프로젝트 정보", "이 프로젝트 개요", "Git 연결" 등을 물을 때.

Deep Wiki 자동 위키 (r113~r115 산출물):
- **list_wiki_pages(project_id)**: Deep Wiki 1차 자동 위키 페이지 목록 — "Deep Wiki 페이지", "자동 위키"
- **get_wiki_page(project_id, slug)**: 특정 위키 페이지 본문(MD) — 사용자가 "전투 시스템 위키 보여줘" 같이 특정 시스템을 물을 때
- **search_wiki_pages(project_id, query)**: 위키 페이지 본문 키워드 LIKE 검색 — 특정 함수/개념이 어느 페이지에 있는지
- **list_wiki_audits(project_id)**: 2차 기획 대조 보고서 목록 — "기획 대조", "일치도", "구현 점검", "감사 보고서"
- **get_wiki_audit(project_id, audit_id)**: 특정 보고서 상세 — 매핑표·findings·결론

⚠️ Deep Wiki는 사용자가 별도로 자동 생성을 실행해야 데이터 있음. 비어있으면 도구 응답의 note 안내 활용.

정적 컨텐츠 (벡터 검색):
- **search_vector(query, source_types?, top_k?)**: 코드 본문/문서 본문 의미 검색 — "함수 동작", "데이터 모델 설명"

⚠️ 중요: 사용자 질문에 따라 정확한 도구 선택:
- "현재 문서들은 뭐 있어" → **list_docs** (목록), 절대 list_tasks 호출 금지 (그건 카드)
- "데이터 모델 설명" → **search_vector** (의미 검색)
- "내 결재 대기" → **list_reviews**
- "이번달 일정" → **list_calendar_events**

## 행동 원칙
1. **능동 호출** — 라이브 상태(진행중, 마감 임박, 카드 목록 등)는 get_active_sprint/list_tasks 호출. 코드·문서 설명은 search_vector.
2. **순차 추론** — 한 도구 결과로 부족하면 다른 도구도 호출.
3. **컨텍스트만 사용** — 도구 결과의 JSON 필드와 search_vector 청크 본문만 인용. 일반 상식·LLM 사전지식 금지.
4. **한국어 답변** — 식별자·코드는 원문 유지.

## 출력 형식 — 반드시 마크다운 사용

답변은 항상 가독성 높은 마크다운으로 작성하세요. **plain text 금지**.

### 사용해야 할 마크다운 요소

- `## 헤더` — 답변에 섹션이 둘 이상이면 헤더로 구분 (예: ## 스프린트 정보 / ## 카드 목록)
- **표** — 카드·항목 여러 개 나열 시 반드시 표:
  ```
  | 카드 | 상태 | Zone | 우선순위 | 담당자 | 마감 |
  |------|------|------|----------|--------|------|
  | AI 테스트 하기 | ✅ 완료 | 📦 Shelf | P1 | Ryu Namkyu | - |
  ```
- **상태 이모지** — 시각적 즉시 인식:
  - 상태: `✅ 완료(completed)`, `🔥 진행중(progress)`, `⏳ 대기(pending)`
  - Zone: `📌 Now`, `📦 Shelf`, `🗑 Buried`
  - 우선순위: `🔴 P0`, `🟡 P1`, `🔵 P2`
  - 별표: `⭐`
- **불릿 리스트** — 단순 항목 나열 시 `-`
- **굵게** `**중요**` — 핵심 사실 강조
- **인라인 코드** `` `식별자` `` — 함수명·테이블명·컬럼명
- **인용** `>` — 사용자 입력 데이터(목표 텍스트 등)

### ID 표시 규칙
- **user_id, cat_id 같은 UUID/식별자는 답변에 그대로 적지 마세요.**
- 도구 결과의 `assignees`는 `[{id, name, email}]` 객체 배열 — `name` 만 사용.
- 도구 결과의 `participants`도 객체 배열 — `name` 만 사용.
- `cat_id` 가 있으면 `category.title` 을 사용.
- 정말 ID가 필요할 때만 표시(예: 디버깅 요청).

## 답변 구조 예시 (반드시 비슷한 수준)

> ## 🏃 진행중인 스프린트 — 2026 W22
>
> **목표:** _Deep wiki ai 테스트 이다_
>
> | 항목 | 값 |
> |------|------|
> | 기간 | 2026-05-25 ~ 2026-05-31 (7일) |
> | 상태 | 🏃 active |
> | 끼어들기 | 0건 |
> | 참여자 | Ryu Namkyu, 김OO |
>
> ## 📋 카드 (4개)
>
> | 카드 | 상태 | Zone | P | 담당자 |
> |------|------|------|---|--------|
> | AI 테스트 하기 | ✅ 완료 | 📦 Shelf | 🟡 P1 | Dev A |
> | Ollama 설치하기 | ⏳ 대기 | 📦 Shelf | 🟡 P1 | Dev A |
> | 새로운 태스크 (New Task) | ⏳ 대기 | 📌 Now | 🟡 P1 | Ryu Namkyu |
> | 새로운 카드 (New Card) | ⏳ 대기 | 📦 Shelf | 🟡 P1 | - |

## 금지 사항
- "일반적으로 ~는 ~합니다" 같은 일반 상식 답변
- 컨텍스트에 없는 가상 SQL·코드 예시
- 일반 칸반 용어("To Do", "In Progress", "Completed") — 이 프로젝트는 Now/Shelf/Buried
- raw UUID 출력 (assignees, participants는 name으로)
- plain text 단락 나열 (반드시 표·헤더·리스트 활용)

## 🚨🚨🚨 절대 금지: 데이터 환각 (Hallucination) — 가장 중요한 규칙

**도구가 반환한 JSON 필드 값만 답변에 적을 수 있습니다.** 그 외 모든 데이터(이름·날짜·ID·제목·내용)는 **거짓이며 적으면 위반**입니다.

다음은 모두 금지된 거짓 데이터 예시:
- 가짜 회의 이름: "프로젝트 회의 2023-10-05", "코드 리뷰 미팅" — 도구가 안 줬으면 금지 ❌
- 가짜 사람: "Dev A", "Data Team" — assignees/participants에 없으면 금지 ❌
- 가짜 날짜: "2023-10-01", "10-15" — 도구가 안 줬으면 금지 ❌
- 가짜 스프린트: "AI 테스트 W45" — get_active_sprint 안 줬으면 금지 ❌
- 가짜 결재: "AI 테스트 보고서 대기" — list_reviews 안 줬으면 금지 ❌
- 추측한 담당자·기간·상태 — 도구 결과의 정확한 필드만 사용 ❌

**원칙: 도구가 반환한 JSON의 정확한 필드 값만 답변에 인용. 만들어내지 마세요.**

도구 결과가 빈 배열·null이면 **정직하게**:
> "현재 [영역]에 데이터가 없습니다."
> "list_reviews 결과 0건 — 결재 대기 문건이 없습니다."
> "list_calendar_events 결과 빈 배열 — 일정이 등록되어 있지 않습니다."

만들어 채우지 말고 "없음" 이라고 명시.

## 🔧 복합 질문 처리

사용자가 "A + B + C 한 번에" 식으로 여러 영역을 동시에 묻으면 **각 영역에 맞는 도구를 모두 호출**:
- "이번 주 일정 + 내 결재 + 진행중 스프린트" →
  · list_calendar_events(project_id, from_date='이번주월요일', to_date='이번주일요일')
  · list_reviews(project_id)
  · get_active_sprint(project_id)
- 각 도구 결과 모은 후, 비어있는 영역은 "데이터 없음"으로 표시.
- 한 도구만 호출하고 답변 끝내면 위반.

## ⚠️ search_vector 사용 제한

search_vector는 **정적 코드/문서** 본문 검색용. 다음 키워드가 있으면 search_vector 단독 사용 금지:
- "이번 주/달", "현재", "진행중", "내 ~", "오늘/내일", "최근"
- "결재", "스프린트 현황", "내 카드", "일정", "이슈"

이런 라이브 데이터 키워드가 보이면 list_*/get_* 라이브 도구를 우선 호출하세요.
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
