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


# [r97→r105] 컨텍스트 품질 게이트 — 매칭이 약하면 서버가 직접 정형응답.
# LLM은 게이트 통과한 컨텍스트로만 답변하므로 "정형응답 옵션"을 가지지 않음 → 결정 책임 분리.
# r97: avg=0.40, max=0.50. r105 강화: avg=0.55, max=0.65.
_GATE_AVG_SIM = 0.55
_GATE_MAX_SIM = 0.65


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
    """[r97→r105] 마지막 user message 앞에 강제 지침을 prefix로 끼워넣음.

    r105 핵심 변경:
    - "정형 응답" 옵션 제거 (서버 게이트가 약한 컨텍스트는 미리 잘라냄)
    - 예시 문장 ("예: 인덱스에서 「칸반 데이터 모델」...") 제거 — LLM이 그대로 베끼는 원인
    - LLM은 도착한 컨텍스트로만 답변. 모호하면 그것조차 솔직히 답.
    """
    prefix = f"""[엄격 지시]

답변은 위에 첨부된 「검색된 컨텍스트」 청크의 본문 텍스트에만 근거해야 합니다.

## 적극적으로 활용할 단서
- 컨텍스트의 함수명/메서드명 (예: dbUpsertCategory, renderBoard)
- 테이블명/컬럼명 (예: kanban_categories, sprint_id, zone)
- 상수·필드명 (예: Shelf, Now, Buried, intrusionCount)
- 타입 정의/스키마 단편 (payload 객체, JSDoc 주석 등)

사용자 질문의 정확한 단어가 청크에 없어도, 위 단서를 묶어 추론 답변하세요.

## 절대 금지
- 일반 상식 답변 ("일반적으로 ~는 ~합니다", "추정으로는…", "보통 ~는 ~")
- 컨텍스트에 없는 가상 SQL/코드 예시
- 일반 칸반 용어 ("To Do", "In Progress", "Completed") — 이 프로젝트는 Now/Shelf/Buried
- 컨텍스트에 등장하지 않은 컬럼명·함수명 만들어내기
- 답변에 literal placeholder 출력 (예: <질문 키워드>, {{q}}, {{query}})
- 사용자가 묻지 않은 단어/주제를 답변 본문에 임의로 끼워 넣기 (특히 첫 줄에서)

## 답변 작성

1) 컨텍스트에서 사용자 질문과 직간접 관련된 모든 단서를 모음.
2) 그 단서를 묶어 답. 짧은 코드 발췌(코드블록)와 [N] 출처 인용 활용.
3) 사용자 질문이 모호하면(예: "??") "구체적으로 어떤 부분을 알고 싶으신가요? 예: ..." 식으로 컨텍스트 청크에서 관련 주제 1~3개를 제시하며 되묻기.
4) 단서가 정말 적으면 있는 만큼만 솔직히 답. "추정으로 채우지 말 것".

마지막 줄: "참조: [1] <실제 파일 경로>" 형태.

이제 사용자 질문:

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
        # [r105] OR 조건으로 강화 — 둘 다 약하면 게이트 발동
        if avg < _GATE_AVG_SIM or mx < _GATE_MAX_SIM:
            # 너무 약함 — LLM에 보내지 않고 정형 응답 직접 생성 (사용자 query 포함)
            files = []
            seen = set()
            for c in chunks[:5]:
                sid = c.get("source_id") or "?"
                if sid in seen:
                    continue
                seen.add(sid)
                files.append(sid)
            # 사용자 query 안전 표시 (escape — markdown 깨짐 방지)
            safe_q = (query or "").replace("`", "'").strip()[:80]
            msg = (
                f"## 인덱스 검색 결과 약함\n\n"
                f"「**{safe_q}**」에 대한 검색 결과가 충분히 관련성 있지 않습니다 "
                f"(평균 유사도 {avg:.2f}, 최대 {mx:.2f} — 게이트: 평균≥{_GATE_AVG_SIM}·최대≥{_GATE_MAX_SIM}).\n\n"
                f"**관련 가능성 있는 파일** (직접 확인 권장):\n"
            ) + "\n".join(f"- `{f}`" for f in files) + (
                f"\n\n💡 더 구체적인 키워드(예: 함수명·테이블명 원문, 한국어 도메인 용어)로 다시 질문해 주세요. "
                f"또는 ⚙️ 설정 → 📋 태스크 인덱싱 / 🏃 스프린트 인덱싱이 최신인지 확인하세요."
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
