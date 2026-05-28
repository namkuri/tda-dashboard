"""RAG — 프롬프트 조립 + LLM 응답 스트리밍."""
from typing import AsyncIterator, List, Dict, Any, Optional
from ollama_client import get_ollama
from retriever import retrieve


# [r94] 프롬프트 완화 — 너무 보수적으로 "모른다"는 답변이 자주 나오는 문제 수정.
# 핵심 변화:
#   · "컨텍스트에 없으면 모른다"를 "최대한 활용해서 답변" 으로 전환.
#   · 코드/태스크 청크에서 파일명·함수명·필드명을 추출해 부분 답변하도록 명시.
#   · 정말 단서 0개일 때만 한정적으로 "충분한 정보 없음" 허용.
SYSTEM_PROMPT = """당신은 TDA Dashboard 프로젝트의 RAG 코드 어시스턴트입니다.
사용자 질문에 대해, 아래 「검색된 컨텍스트」를 단서로 활용해 가능한 한 구체적으로 답변합니다.

원칙:
1. **단서 활용** — 컨텍스트에 직접적인 답이 없어도, 파일명·함수명·테이블명·필드명에서 단서를 추출해 부분 답변하세요. 단서가 있으면 "확실하지 않지만 ~로 보입니다" 표현으로 추정 답변을 제공.
2. **출처 인용** — 답변에 사용한 청크는 `[1]`, `[2]` 형태로 인라인 인용. 컨텍스트 머리말 `### [N]` 의 번호 사용.
3. **코드 인용** — 짧은 발췌(5~20줄)는 ```언어 코드블록으로 표시. 긴 함수는 시그니처+설명만.
4. **한국어 답변** — 식별자/코드는 원문 유지. 문법 용어는 한국어로.
5. **솔직함의 한계** — 컨텍스트가 완전히 무관한 경우에만 "현재 인덱스에서 직접 관련 정보를 찾지 못했습니다. 추정으로는…" 라고 시작하되, 그래도 파일명/함수명 단서로 최선의 추측을 시도하세요.
6. **답변 구조** — 짧으면 1~3문단, 길면 ## 소제목으로 구분. 마지막 줄에 "참조: [1] file, [2] file" 1줄 요약.

목표: "모르겠다"는 답변을 최소화하고, 부분 정보라도 사용자가 다음 검색·읽기를 시작할 수 있게 출처와 함께 제시.
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
