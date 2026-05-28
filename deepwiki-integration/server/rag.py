"""RAG — 프롬프트 조립 + LLM 응답 스트리밍."""
from typing import AsyncIterator, List, Dict, Any, Optional
from ollama_client import get_ollama
from retriever import retrieve


SYSTEM_PROMPT = """당신은 TDA Dashboard 프로젝트의 RAG 챗봇입니다.
주어진 컨텍스트(코드, 위키 문서, 태스크, 스프린트)를 바탕으로 정확하고 구체적인 답변을 제공합니다.

규칙:
1. 컨텍스트에 명시된 사실만 답변하세요. 추측은 "확실하지 않지만..." 표시.
2. 코드 인용 시 마크다운 코드블록 사용 (언어 태그 포함).
3. 답변 마지막에 어떤 소스를 참조했는지 한 줄로 요약하세요.
4. 한국어로 답변. 단, 코드/식별자는 원문 유지.
5. 컨텍스트에 없으면 "관련 문서·코드가 인덱싱돼 있지 않습니다"라고 솔직히 답변.
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


async def chat_stream(
    messages: List[Dict[str, str]],
    project_id: Optional[str] = None,
    model: Optional[str] = None,
    include_tasks: bool = True,
) -> AsyncIterator[Dict[str, Any]]:
    """RAG 스트리밍 응답.

    1. 마지막 user 메시지로 retrieve
    2. 컨텍스트 블록 + 대화 히스토리 + system 프롬프트로 LLM 호출
    3. yield {"delta": "..."} ... {"sources": [...]}
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

    # 2. 컨텍스트 조립
    context = build_context_block(chunks)

    # 3. 메시지 조립 — 시스템 프롬프트 + 컨텍스트 + 대화 히스토리
    llm_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"## 검색된 컨텍스트\n\n{context}\n\n위 컨텍스트를 참조해 답변하세요."},
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
