"""RAG — 프롬프트 조립 + LLM 응답 스트리밍."""
from typing import AsyncIterator, List, Dict, Any, Optional
from ollama_client import get_ollama
from retriever import retrieve


# [r96→r97] 시스템 프롬프트 — Qwen 2.5 Coder는 system을 약하게 따르는 경향이라
# r97에서는 매 user message 앞에도 강제 지침을 prefix로 추가(아래 _enforce_user_message).
# system은 짧고 명확하게, 강제력은 user에서.
SYSTEM_PROMPT = """당신은 TDA Dashboard 프로젝트 전용 RAG 어시스턴트입니다.
유일한 정보원은 사용자 메시지에 첨부된 「검색된 컨텍스트」입니다.
일반 상식·LLM 사전지식은 절대 사용하지 않습니다.
한국어로 답합니다. 코드/식별자는 원문 유지.
"""


# [r97] 컨텍스트 품질 게이트 — 매칭이 너무 약하면 LLM 호출 자체를 우회.
# LLM이 "추정으로는…" 일반 상식으로 빠지는 것을 방지.
_GATE_AVG_SIM = 0.40
_GATE_MAX_SIM = 0.50


def build_context_block(chunks: List[Dict[str, Any]]) -> str:
    """검색된 청크들을 LLM 프롬프트의 컨텍스트 블록으로 조립."""
    if not chunks:
        return "(관련 컨텍스트 없음)"
    lines = []
    for i, c in enumerate(chunks, 1):
        kind = c.get("source_type", "?")
        title = c.get("source_title") or c.get("source_id", "?")
        sim = c.get("similarity", 0)
        match_kind = c.get("_match_kind", "")
        match_tag = f" · {match_kind}" if match_kind else ""
        content = c.get("content", "")[:1200]
        lines.append(f"### [{i}] {kind} · {title} (유사도 {sim:.2f}{match_tag})\n{content}")
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


def _enforce_user_message(user_content: str, chunks: List[Dict[str, Any]]) -> str:
    """[r97→r100] 마지막 user message 앞에 강제 지침을 prefix로 끼워넣음.

    r97는 너무 엄격했음 — 사용자 질문 키워드의 "정확한 텍스트"가 청크에 없으면
    LLM이 정형 응답으로 빠짐. r100에서 완화: 함수명·테이블명·필드명 같은 단서가
    있으면 그것을 활용해 추론 답변 허용. 단 컨텍스트 외 상식은 여전히 금지.
    """
    src_list = []
    seen_sources = set()
    for c in chunks[:6]:
        sid = c.get("source_id") or "?"
        if sid in seen_sources:
            continue
        seen_sources.add(sid)
        src_list.append(f"`{sid}`")
    prefix = f"""[엄격 지시 — 이 지시 위반 답변은 사용자가 거부합니다]

당신의 답변은 위에 첨부된 「검색된 컨텍스트」 청크의 본문에 근거해야 합니다.

## 적극적으로 활용할 단서 (있으면 반드시 인용)
- 컨텍스트 청크 안의 **함수명·메서드명** (예: `dbUpsertCategory`, `renderBoard`)
- 청크 안의 **테이블명·컬럼명** (예: `kanban_categories`, `sprint_id`, `zone`)
- 청크 안의 **상수/필드명** (예: `Shelf`, `Now`, `Buried`, `intrusionCount`)
- 청크 안의 **타입 정의/스키마 단편** (예: `payload = {{ id, project_id, ... }}`)

이런 단서가 청크에 있으면, 사용자 질문의 정확한 단어가 없더라도 그 단서를 묶어 답하세요.
예: 컨텍스트에 `dbUpsertCategory`와 `kanban_categories` 가 보이면, 그것이 곧 "칸반 데이터 모델"의 일부이므로 인용·요약해서 답.

## 절대 금지
- "일반적으로 …는 …합니다" 형태의 일반 상식 답변
- "추정으로는…", "보통 …는 …" 같은 일반화 표현
- 컨텍스트에 없는 가상 SQL 예시 (예: `CREATE TABLE tasks (id INT, ...)` 같은 사전지식 기반 코드)
- 일반 칸반 용어("To Do", "In Progress", "Completed") — 이 프로젝트는 Now/Shelf/Buried 사용
- 컨텍스트에 등장하지 않은 컬럼명·함수명 만들어내기

## 답변 절차

1) 컨텍스트 청크들을 한 번 훑어 사용자 질문과 관련된 **단서**를 모두 모읍니다.
2) 단서 1개 이상 있으면 → 그것을 묶어서 답변. 짧은 코드 발췌(```언어 블록)와 `[N]` 출처 인용.
3) 단서가 전혀 없거나 청크들이 모두 무관한 내용이면 → 다음 형식으로 답하세요 (반드시 실제 사용자 키워드와 실제 파일 경로로 치환):

   - 첫 줄: "인덱스에서 「{q}」에 대한 정보를 찾지 못했습니다." (여기서 {q}는 사용자의 실제 키워드)
   - 다음에 "관련 가능성 있는 파일:" 헤더
   - 그 아래 위 컨텍스트의 실제 파일 경로 1~3개 bullet
   - 마지막에 "더 구체적인 키워드로 다시 질문해 주세요"

**절대 금지**: "<질문 키워드>" 같은 placeholder 문자열을 답변에 literal로 출력하면 안 됨 — 반드시 사용자의 진짜 질문 단어로 치환.

**중요**: 정형 응답은 정말로 단서가 0개일 때만. 단서가 있는데도 정형 응답을 선택하면 위반.

마지막 줄에 "참조: [1] <path>, [2] <path>" 형태로 출처 요약.

이제 다음 질문에 답하세요:

{user_content}"""
    return prefix


async def chat_stream(
    messages: List[Dict[str, str]],
    project_id: Optional[str] = None,
    model: Optional[str] = None,
    include_tasks: bool = True,
) -> AsyncIterator[Dict[str, Any]]:
    """RAG 스트리밍 응답."""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        yield {"delta": "❌ 질문이 없습니다."}
        return
    query = user_msgs[-1]["content"]

    # 1. Retrieve (하이브리드 — 벡터 + LIKE)
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

    # 진단 메타 송신 — [r102] query도 포함해 클라이언트가 placeholder 치환에 사용
    meta_data = _retrieval_meta(chunks)
    meta_data["query"] = query
    yield {"meta": meta_data}

    # [r97] 컨텍스트 품질 게이트 — 약한 매칭은 LLM 호출 우회
    if chunks:
        sims = [c.get("similarity", 0) for c in chunks]
        avg = sum(sims) / len(sims)
        mx = max(sims)
        if avg < _GATE_AVG_SIM and mx < _GATE_MAX_SIM:
            # 너무 약함 — LLM에 보내지 않고 정형 응답 직접 생성
            files = []
            seen = set()
            for c in chunks[:5]:
                sid = c.get("source_id") or "?"
                if sid in seen:
                    continue
                seen.add(sid)
                files.append(sid)
            msg = (
                f"## 인덱스 검색 결과 약함\n\n"
                f"검색된 청크들이 질문과 의미적으로 충분히 관련성이 없습니다 "
                f"(평균 유사도 {avg:.2f}, 최대 {mx:.2f}).\n\n"
                f"**관련 가능성 있는 파일** (직접 확인 권장):\n"
            ) + "\n".join(f"- `{f}`" for f in files) + (
                f"\n\n더 구체적인 키워드(예: 함수명·테이블명 원문)로 다시 질문해 주세요."
            )
            yield {"delta": msg}
            # 출처는 그래도 노출
            sources = []
            for c in chunks[:5]:
                st = c.get("source_type", "?")
                sid = c.get("source_id", "?")
                ftype = {"code": "doc", "wiki": "doc", "task": "task", "sprint": "sprint"}.get(st, "doc")
                sources.append({"type": ftype, "id": sid, "title": c.get("source_title") or sid})
            if sources:
                yield {"sources": sources}
            return

    # 2. 컨텍스트 조립
    context = build_context_block(chunks)

    # 3. [r97] user message에 강제 지침 prefix 추가 (마지막 user만)
    enforced_messages = []
    last_user_idx = -1
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            last_user_idx = i
    for i, m in enumerate(messages):
        if i == last_user_idx:
            enforced_messages.append({"role": "user", "content": _enforce_user_message(m["content"], chunks)})
        else:
            enforced_messages.append(dict(m))

    # 4. 메시지 조립
    llm_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"## 검색된 컨텍스트\n\n{context}"},
    ]
    llm_messages.extend(enforced_messages[-6:])

    # 5. LLM 스트리밍 — placeholder 치환은 클라이언트 측에서 (스트리밍 단위로 깔끔히 처리)
    ollama = get_ollama()
    try:
        async for delta in ollama.chat_stream(messages=llm_messages, model=model):
            yield {"delta": delta}
    except Exception as e:
        yield {"delta": f"\n\n❌ LLM 호출 실패: {e}\n\nOllama가 실행 중인지, 모델이 pull됐는지 확인하세요."}
        return

    # 6. 출처 메타
    sources = []
    for c in chunks[:5]:
        st = c.get("source_type", "?")
        sid = c.get("source_id", "?")
        ftype = {"code": "doc", "wiki": "doc", "task": "task", "sprint": "sprint"}.get(st, "doc")
        sources.append({
            "type": ftype,
            "id": sid,
            "title": c.get("source_title") or sid,
        })
    if sources:
        yield {"sources": sources}
