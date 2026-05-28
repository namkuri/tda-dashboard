"""RAG — 프롬프트 조립 + LLM 응답 스트리밍."""
from typing import AsyncIterator, List, Dict, Any, Optional
from ollama_client import get_ollama
from retriever import retrieve


# [r96] 프롬프트 강화 — r94에서 "추정 답변" 허용이 너무 헐거워서, LLM이 컨텍스트를 무시하고
# 일반 지식("'To Do', 'In Progress', 'Completed'와 같은 열…")으로 답하는 문제 해결.
# 핵심 원칙: 일반 지식 답변 절대 금지. 컨텍스트 청크에서 직접 발췌·인용만. 정보 없으면
# 어떤 파일을 직접 열어봐야 하는지 안내.
SYSTEM_PROMPT = """당신은 TDA Dashboard 프로젝트 전용 RAG 코드 어시스턴트입니다.
당신의 유일한 정보원은 아래 「검색된 컨텍스트」입니다. 일반 상식·LLM 사전지식은 **절대 사용 금지**.

## 답변 규칙 (위반하면 응답을 폐기하세요)

1. **컨텍스트만 사용** — 컨텍스트 청크에 실제로 적혀있는 내용만 인용·요약. "일반적으로 …는 …합니다" 같은 상식 답변 절대 금지. "추정으로는" / "보통 …는 …" 같은 일반화 표현 금지.

2. **직접 인용** — 답변에 쓰는 모든 사실은 컨텍스트 청크에서 찾을 수 있어야 함. 청크 텍스트를 짧게 발췌(```코드 블록 또는 인용문)하고 `[N]`으로 출처 번호 표시. 컨텍스트 머리말 `### [N]` 번호 사용.

3. **컨텍스트에 답이 없으면** — 절대 추측하지 말고 다음 형식으로 답:

   > 인덱스에서 「<질문 키워드>」에 대한 직접적인 정보를 찾지 못했습니다.
   >
   > 다만 다음 파일들이 관련 가능성 있습니다 — 직접 열어 확인하세요:
   > - `<source_id 1>` ([N1])
   > - `<source_id 2>` ([N2])
   >
   > 더 구체적인 질문(예: 함수명 직접 입력)을 시도해 보시거나, 해당 파일에 직접 데이터가 있는지 확인해 주세요.

4. **부분 정보** — 컨텍스트에 일부만 있으면, 있는 부분만 명확히 답하고 "다음은 인덱스에 없습니다: …" 로 누락분 명시.

5. **코드 인용** — 짧은 발췌(5~20줄)는 ```언어 코드블록. 변수명·함수명·테이블명은 원문 유지(한국어 번역 금지).

6. **답변 구조** — 한국어. 짧으면 1~3문단, 길면 ## 소제목. 마지막에 `참조: [1] <path>, [2] <path>` 1줄.

## 좋은 답변 예시 (컨텍스트가 dbUpsertCategory를 포함했을 때)

> `dbUpsertCategory(cat)` 는 `kanban_categories` 테이블에 카테고리를 upsert합니다 [1]:
> ```js
> const payload = { id: cat.id, project_id: ..., sprint_id: cat.sprintId, ... };
> ```
> 컬럼: `id`, `project_id`, `title`, `subtitle`, `sprint_id`, `owner_user_id` [1].
> 참조: [1] public/index.html

## 나쁜 답변 예시 (절대 이렇게 답하지 마세요)

> 일반적으로 칸반 보드 애플리케이션에서 카테고리 테이블은 id, name, description, created_at 컬럼을 포함할 수 있습니다.

← 컨텍스트에 없는 일반 상식 — 폐기.
"""


def build_context_block(chunks: List[Dict[str, Any]]) -> str:
    """검색된 청크들을 LLM 프롬프트의 컨텍스트 블록으로 조립."""
    if not chunks:
        return "(관련 컨텍스트 없음)"
    lines = []
    for i, c in enumerate(chunks, 1):
        kind = c.get("source_type", "?")
        title = c.get("source_title") or c.get("source_id", "?")
        sim = c.get("similarity", 0)
        content = c.get("content", "")[:1200]  # 너무 길면 잘라냄
        lines.append(f"### [{i}] {kind} · {title} (유사도 {sim:.2f})\n{content}")
    return "\n\n---\n\n".join(lines)


def _retrieval_meta(chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """[r94] 검색 진단 메타 — 프론트에서 칩으로 표시."""
    if not chunks:
        return {"retrieved": 0, "avg_sim": 0.0, "max_sim": 0.0, "min_sim": 0.0, "top": []}
    sims = [c.get("similarity", 0) for c in chunks]
    top = []
    for c in chunks[:5]:
        top.append({
            "type": c.get("source_type", "?"),
            "title": (c.get("source_title") or c.get("source_id", "?"))[:60],
            "sim": round(c.get("similarity", 0), 3),
        })
    return {
        "retrieved": len(chunks),
        "avg_sim": round(sum(sims) / len(sims), 3),
        "max_sim": round(max(sims), 3),
        "min_sim": round(min(sims), 3),
        "top": top,
    }


async def chat_stream(
    messages: List[Dict[str, str]],
    project_id: Optional[str] = None,
    model: Optional[str] = None,
    include_tasks: bool = True,
) -> AsyncIterator[Dict[str, Any]]:
    """RAG 스트리밍 응답.

    1. 마지막 user 메시지로 retrieve
    2. 컨텍스트 블록 + 대화 히스토리 + system 프롬프트로 LLM 호출
    3. yield {"meta": {...}} → {"delta": "..."} ... {"sources": [...]}
    """
    # 마지막 user 메시지 추출
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        yield {"delta": "❌ 질문이 없습니다."}
        return
    query = user_msgs[-1]["content"]

    # 1. Retrieve
    source_types = ["code", "wiki", "sprint"]
    if include_tasks:
        source_types.append("task")
    try:
        chunks = await retrieve(
            query=query,
            project_id=project_id,
            source_types=source_types,
        )
    except Exception as e:
        yield {"delta": f"❌ 검색 실패: {e}\n\nDB·임베딩 모델 상태를 확인하세요."}
        return

    # [r94] 검색 진단 메타 — LLM 호출 전에 먼저 송신 (사용자가 검색 상태 즉시 확인)
    yield {"meta": _retrieval_meta(chunks)}

    # 2. 컨텍스트 조립
    context = build_context_block(chunks)

    # 3. 메시지 조립 — 시스템 프롬프트 + 컨텍스트 + 대화 히스토리
    llm_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"## 검색된 컨텍스트\n\n{context}\n\n위 컨텍스트의 단서를 적극 활용해 답변하세요."},
    ]
    # 최근 6개 메시지만 (토큰 절약)
    llm_messages.extend(messages[-6:])

    # 4. LLM 스트리밍
    ollama = get_ollama()
    try:
        async for delta in ollama.chat_stream(messages=llm_messages, model=model):
            yield {"delta": delta}
    except Exception as e:
        yield {"delta": f"\n\n❌ LLM 호출 실패: {e}\n\nOllama가 실행 중인지, 모델이 pull됐는지 확인하세요."}
        return

    # 5. 출처 메타 — 응답 끝에 SSE로 전송
    sources = []
    for c in chunks[:5]:  # 상위 5개만 표시
        st = c.get("source_type", "?")
        sid = c.get("source_id", "?")
        # TDA 프론트엔드의 xpJump-호환 type 매핑
        ftype = {"code": "doc", "wiki": "doc", "task": "task", "sprint": "sprint"}.get(st, "doc")
        sources.append({
            "type": ftype,
            "id": sid,
            "title": c.get("source_title") or sid,
        })
    if sources:
        yield {"sources": sources}
