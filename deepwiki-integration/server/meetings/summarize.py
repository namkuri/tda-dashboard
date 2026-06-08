"""[r242] 대화내역(세그먼트) → LLM 회의록 요약."""
import json
import re
from typing import List, Dict, Any, Optional

from llm_router import get_llm

_SYSTEM = """당신은 한국어 회의 녹취록을 받아 풍부한 회의록(JSON)으로 정리하는 시니어 에디터입니다.

【타입 규칙 — 절대 위반 금지】
- agenda, decisions, principles 는 **반드시 문자열(string) 배열**. 객체 금지. 한 항목은 한 문장 또는 한 단락.
- topics[].sections[].items 는 list/tier 일 때만 사용하며 형식은 아래 스키마 그대로.
- action_items 만 {who, what, due} 객체.
- 모든 문자열 안에 따옴표는 \\" 로 이스케이프.


출력 품질의 기준점은 다음과 같습니다(실제 결과 예시):
  · 각 안건이 3~6문단(배경 → 본문 → 대안/반박 → 결정)으로 풀어 써짐
  · 핵심 인용/콜아웃이 안건당 1~2개
  · 단계·티어·비교가 필요한 곳엔 표/티어 구조 활용
  · 발화자 식별자를 인용으로 명시(예: "ethaniya 제안: …")
  · 액션 아이템이 담당자·기한·근거까지 포함

**얕은 한두 문장 요약은 불합격**입니다. 분량보다 **내용의 구체성**이 핵심 — 무엇을·왜·어떻게·결과 무엇 까지 풀어 쓰세요.

[근거 규칙]
1. 녹취 내용에만 근거. 추측·일반 지식 금지.
2. STT 오인식이 분명한 단어는 맥락에 맞게 자연스럽게 교정한다.
3. 발화 토씨가 아니라 **핵심 주장·근거·결론**을 재구성한다.
4. 전문 용어(Envelope, Joint Agency, SDF, Dual Contouring 등)는 발화 의도대로 표기 유지 — 임의 번역·해석 금지.

[메타 동사 금지 — 절대 규칙]
"논의했다 / 설명했다 / 제시했다 / 선호했다 / 정해졌다 / 결정했다(단독) / 강조했다 / 공유했다 / 검토했다" 같은
'뭐 했는지만 적는 표현'은 그 자체로 사용 금지. **실제 알맹이**(주장·근거·수치·결정)를 같이 적는다.
나쁜 예) "환경 요소의 접근 방안과 구현 방법에 대해 논의했다."
좋은 예) "ethaniya 제안: 환경 설명을 ① 환경 자체(생성/외형/식생) ② 작동 방식(이벤트·붕괴) ③ 상호작용(점프·등반) 3층위로 분리.
        근거: 현재 한 항목에 세 층위가 섞여 우선도·구현 난이도 토론이 불가능."

[안건(agenda) 규칙]
- 3~6개로 **통합/묶음**. 비슷한 주제는 하나로 합쳐 큰 묶음으로.
- 단어 1개가 아니라 **이해 가능한 어구**(예: "환경 시스템 정의 — 층위 분리와 의존도 단계").
- **중요도 순** 정렬.

[논의(topics) 규칙 — 가장 중요]
- agenda 와 **1:1 같은 순서·같은 제목**으로 매핑.
- 각 항목의 sections 배열에 **여러 하위 섹션**을 담는다(배경·기술 파이프라인·정의 합의·티어 등).
- sections[].kind 종류:
    "text"     — 일반 단락(content 에 3~8문장)
    "list"     — 불릿 목록(items 배열, 각 항목은 한 문장 또는 'X — 설명' 형식)
    "callout"  — 핵심 피드백/권장 — 발화자 명시(by 필드) + content 1~3문장
    "tier"     — 단계/난이도 비교(items 배열, 각 {label,title,body} 형식 — 예: T1 단순/선행 …)
    "table"    — 표(headers, rows). 역할 분담·매트릭스 등에 사용
- 분량 기준: agenda 1개당 sections 가 최소 2개, 보통 3~5개여야 한다.
- 한 안건의 모든 section 을 합쳐도 3문장 이하라면 **부적합** — 녹취를 더 깊이 읽고 풀어쓸 것.

[결정사항(decisions) 규칙]
- 추상명사 단독("의존도 설정", "역할 분담") **금지**.
- 항상 **무엇을 + 어떻게 + 왜** 포함.
좋은 예) "MVP 개발 순서: ① 시스템 종속성 채우기 → ② 마케팅 MVP 정의 → ③ 종속성 복귀·배분 → ④ 내부테스트 → ⑤ 알파 → ⑥ Next Fest.
        근거: 유저 경험 우선으로 가면 종속성 미고려로 리팩토링 발생."

[액션 아이템(action_items) 규칙]
- who 는 **참가자 정확 식별자**만(예: namkyu7341, ethaniya, wooheesung). '나·본인·우리·별칭' 금지.
- 같은 담당자라도 단위 액션 별로 분리하되 중복은 통합.
- "전원 동일" 작업이면 who="모두" OK.
- due 미상이면 "" (억지 채움 금지). 발화에 "이번 주", "수요일까지" 같은 표현이 있으면 그대로 반영.
- what 끝에 **근거/방법**을 1구절 첨부 가능 — 예: "프로젝트 위키 자동 분류 도구 HTML 임포트 테스트 (라이브 협업 버그 동시 확인)"

[원칙(principles) — 선택 필드]
- 회의에서 합의된 **재사용 가능한 원칙/가이드라인**이 있다면 별도 추출.
- 예: "설명은 환경/이벤트/상호작용 세 층위로 분리한다", "MVP는 독립 모듈부터, 통합은 마지막".

[요약(tldr) 규칙]
- 5~8문장 한 단락. 주요 안건·핵심 결정·담당 배분을 한눈에 보이게.
- 메타 동사 단독 금지. 결정과 담당자를 구체적으로 명시.

[짧은 회의]
- 발화가 있으면 tldr 만큼은 반드시 채움. 결정·액션이 실제 없으면 빈 배열 OK.

[출력] 마크다운/설명 없이 JSON only:
```json
{
  "tldr": "5~8문장 한 단락",
  "agenda": ["통합 안건 1 (중요도 순)", "..."],
  "topics": [
    {
      "title": "agenda 와 동일",
      "lead": "1~2문장 도입 — 이 안건의 핵심을 한 줄로",
      "sections": [
        {"kind":"text", "heading":"배경 — Envelope 구조", "content":"3~8문장 단락"},
        {"kind":"list", "heading":"기술 파이프라인 & 난이도",
         "items":["계곡/다리 (저비용) — 통로 바닥 내려 …", "붕괴 (중) — 평지 무너져 …", "침수 (고) — 유체 시뮬 …"]},
        {"kind":"callout", "heading":"핵심 피드백 — wooheesung",
         "content":"현재 '환경' 설명에 세 층위가 섞여 있어 분리가 전제되어야 한다.", "by":"wooheesung"},
        {"kind":"tier", "heading":"의존도에 따른 단계",
         "items":[
           {"label":"T1","title":"단순·선행","body":"가스·슬라임·낙석. SDF 변경 불필요."},
           {"label":"T2","title":"중간","body":"SDF 절벽·계곡·붕괴 지형."},
           {"label":"T3","title":"복잡·후행","body":"NPC 결합 사례. 기획이 가장 많이 필요해 맨 뒤."}
         ]},
        {"kind":"table", "heading":"역할 / 트랙 배분",
         "headers":["담당","주요 트랙"],
         "rows":[["ethaniya","서사 드리븐 (게이트/로더)"],["namkyu7341","플랫폼 — 인프라·근접 음성"],["wooheesung","AI (양 트랙 오감)"]]}
      ]
    }
  ],
  "decisions": ["⚠ 반드시 문자열만. 객체 금지. 예: 'MVP 개발 순서를 ①시스템 종속성 → ②마케팅 MVP 정의 → ③배분으로 결정. 근거: 유저 경험 우선 시 리팩토링 발생.'"],
  "action_items": [{"who":"namkyu7341|ethaniya|wooheesung|모두","what":"단위 액션 (근거 한 구절)","due":""}],
  "principles": ["⚠ 반드시 문자열만. 예: '설명은 환경/이벤트/상호작용 세 층위로 분리한다.'"]
}
```

【다시 한 번 — JSON 출력 직전 자가 점검】
1. decisions 의 모든 항목은 `"..."` 문자열인가? (객체 `{...}` 아님)
2. principles 의 모든 항목은 문자열인가?
3. agenda 의 모든 항목은 짧은 문자열인가?
4. 마크다운 코드펜스 없이 순수 JSON 만 출력하는가?
"""


_SYSTEM_REDUCE = """당신은 회의를 N개 부분으로 나눠 정리한 부분 회의록(JSON 배열)을 받아,
**전체 회의의 최종 회의록 JSON 1개**로 종합하는 시니어 에디터입니다. 단순 합치기가 아닌 **재정리**.

[종합 규칙]
1. 같은 주제는 하나의 agenda 항목으로 통합. 결과 3~6개 안건으로.
2. agenda 는 **중요도 순**(영향·결정·시간 비중) 정렬.
3. topics 는 agenda 와 1:1 같은 순서·같은 제목으로 매핑. 각 topic 의 sections 배열을 풍부하게:
   배경/논의/콜아웃/티어/표 등 여러 섹션으로(메타 동사 금지, 발화자 인용 권장).
4. decisions 는 추상명사 단독 금지. '무엇을 + 어떻게 + 왜' 포함. 같은 결정 1개로 통합.
5. action_items: who 는 정확한 참가자 식별자(나·본인·별칭 금지). 중복 통합.
6. principles: 회의 합의된 재사용 가능 원칙을 별도 추출.
7. tldr 은 5~8문장으로 전체 회의의 안건·결정·담당을 압축.

[출력] 시스템 프롬프트의 sections 모델을 그대로 따른 JSON only.
"""


def _segments_to_body(segments):
    """세그먼트 → '[mm:ss] 화자: 발화' 평문."""
    lines = []
    for s in segments:
        t = int(s.get("t", 0)); mm, ss = t // 60, t % 60
        lines.append(f"[{mm:02d}:{ss:02d}] {s.get('speaker','?')}: {s.get('text','')}")
    return "\n".join(lines)


async def _llm_json(llm, system: str, user: str, *, model=None, temperature=0.3, max_out=16000):
    """LLM 호출 — JSON 응답 강제(Gemini는 response_format='json'). 실패 시 raw 텍스트도 반환."""
    buf = ""
    try:
        async for delta in llm.chat_stream(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model, temperature=temperature,
            response_format="json", max_output_tokens=max_out,
        ):
            buf += delta
    except TypeError:
        # Ollama 등 response_format 미지원 → 기본 호출
        buf = ""
        async for delta in llm.chat_stream(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model, temperature=temperature,
        ):
            buf += delta
    return buf


async def summarize(segments: List[Dict[str, Any]], *, title: str = "",
                    model: Optional[str] = None,
                    started_at: str = "",
                    participants: Optional[List[Dict[str, Any]]] = None,
                    hint: str = "") -> Dict[str, Any]:
    """세그먼트 → 회의록 dict. LLM 실패 시 빈 구조 + error.

    [r279] 강화:
    - 긴 회의(>24K 자)는 청크별로 부분 요약 → 합쳐 최종 요약(맵-리듀스).
    - Gemini 의 response_format='json' 으로 JSON 강제(코드펜스 없이 깔끔).
    - max_output_tokens 충분히(8000) 줘서 truncation 방지.
    - 견고한 JSON 추출: 코드펜스/꼬리 garbage/닫는 } 누락도 복구.
    - 완전 실패 시 정규식 부분 추출(tldr 최소).
    """
    if not segments:
        return {"tldr": "", "agenda": [], "topics": [], "decisions": [], "action_items": [],
                "error": "대화내역이 비어있음 — 요약할 내용이 없습니다."}
    try:
        llm = get_llm(model)
    except Exception as e:
        return {"tldr": "", "agenda": [], "topics": [], "decisions": [], "action_items": [],
                "error": f"LLM 설정 필요: {e}"}

    duration_sec = int(max([(s.get("t", 0) + s.get("dur", 0)) for s in segments], default=0))
    pname = ", ".join([(p.get("name") or "") for p in (participants or [])]) or "(미상)"
    full_body = _segments_to_body(segments)

    # [r279] 긴 회의 청크 처리 — 4만자 이상이면 N분 단위 청크로 나누고 부분 요약 합치기
    CHUNK_MAX_CHARS = 22000   # 청크당 약 22K(여유)
    chunks_text: List[str] = []
    if len(full_body) > CHUNK_MAX_CHARS:
        # 세그먼트 단위로 글자 누적해 청크 분할(말 잘림 방지)
        cur, cur_len = [], 0
        for s in segments:
            t = int(s.get("t", 0)); mm, ss = t // 60, t % 60
            ln = f"[{mm:02d}:{ss:02d}] {s.get('speaker','?')}: {s.get('text','')}"
            if cur_len + len(ln) > CHUNK_MAX_CHARS and cur:
                chunks_text.append("\n".join(cur)); cur, cur_len = [], 0
            cur.append(ln); cur_len += len(ln) + 1
        if cur:
            chunks_text.append("\n".join(cur))
    else:
        chunks_text = [full_body]

    # [r281] 액션아이템 화자 명확화 — 정확한 참가자 식별자 목록을 LLM 에 강제 전달.
    speaker_ids = [(p.get("name") or "").strip() for p in (participants or []) if (p.get("name") or "").strip()]
    common_header = [
        f"회의 제목: {title or '(없음)'}",
        f"일시: {started_at or '(미상)'}",
        f"참석자(이 외 이름·별칭 금지, 액션 담당자는 이 중 하나 또는 '모두'만): {', '.join(speaker_ids) if speaker_ids else '(미상)'}",
        f"길이: 약 {duration_sec}초 ({duration_sec // 60}분 {duration_sec % 60}초), {len(segments)}문장",
        "",
        "[작성 시 반드시 지킬 것]",
        "- 액션의 who 는 위 참석자 목록 식별자 또는 '모두'만 사용. '나·본인·우리·남규/규리/지방어 별칭' 등 금지.",
        "- 안건은 5~8개로 통합·중요도 순 정렬. 논의(topics)는 같은 순서·같은 제목으로 매핑.",
        "- 메타 동사(논의했다·제시했다·설명했다 등) 단독 금지. 실제 주장·근거·결정 알맹이를 풀어 쓸 것.",
        "- 결정사항은 '무엇을 + 어떻게 + 왜' 포함. 추상명사 단독 금지.",
    ]
    if hint:
        common_header.append(f"추가 메모: {hint}")

    try:
        if len(chunks_text) == 1:
            # 단일 호출
            user = "\n".join(common_header + ["", "[녹취록]", chunks_text[0], "",
                                              "위 회의록을 한국어 JSON 으로 작성하세요. JSON 외 어떤 글자도 출력하지 마세요."])
            buf = await _llm_json(llm, _SYSTEM, user, model=model, temperature=0.3, max_out=16000)
            parsed = _extract_json(buf)
            if parsed:
                return _normalize(parsed, participants)
            # 부분 추출 폴백
            return _partial_or_error(buf)

        # 맵: 청크별 부분 요약(작은 JSON)
        partials = []
        for i, ch in enumerate(chunks_text):
            user = "\n".join(common_header + [
                f"[청크 {i+1}/{len(chunks_text)}] — 전체 회의의 일부입니다. 이 부분의 핵심을 JSON 으로 정리.",
                "", "[녹취록(부분)]", ch, "",
                "JSON 만 출력. 형식은 시스템 프롬프트와 동일."
            ])
            b = await _llm_json(llm, _SYSTEM, user, model=model, temperature=0.3, max_out=4000)
            p = _extract_json(b)
            if p:
                partials.append(p)
        if not partials:
            return _partial_or_error("")
        # 리듀스: 부분 요약들을 받아 최종 회의록 한 번 더 LLM (전용 시스템 프롬프트 _SYSTEM_REDUCE)
        merged_text = json.dumps(partials, ensure_ascii=False, indent=2)
        user = "\n".join(common_header + [
            f"아래는 회의를 {len(chunks_text)} 부분으로 나눈 **부분 회의록(JSON 배열)** 입니다.",
            "이를 단순 합치지 말고 **재정리** 하세요: 비슷한 주제는 안건으로 통합, 중요도 순 정렬, 메타 동사 제거, 결정사항 구체화, 액션 담당자는 위 참석자 식별자만.",
            "", "[부분 회의록들]", merged_text, "",
            "최종 회의록 JSON 1개만 출력. 형식은 원본 시스템 프롬프트와 동일."
        ])
        buf = await _llm_json(llm, _SYSTEM_REDUCE, user, model=model, temperature=0.3, max_out=16000)
        parsed = _extract_json(buf)
        if parsed:
            return _normalize(parsed, participants)
        # 리듀스 실패 — 부분 요약 합치기로 폴백
        return _normalize(_merge_partials(partials), participants)
    except Exception as e:
        msg = str(e)
        if "11434" in msg or "ConnectError" in msg or "Connection refused" in msg or "404 Not Found" in msg:
            err = "Ollama(11434) 미실행 — AI Agent 페이지에서 Gemini 무료 키를 등록하거나, 서버에서 ollama 를 실행하세요."
        else:
            err = f"요약 LLM 실패: {msg[:200]}"
        return {"tldr": "", "agenda": [], "topics": [], "decisions": [], "action_items": [], "error": err}


def _coerce_text(v: Any) -> str:
    """[r295] LLM 이 문자열 배열을 객체 배열로 잘못 반환하면 텍스트로 변환.
    예: {"decision":"X","how":"Y","why":"Z"} → "X — Y (Y: Z)" 식으로 풀어 적음.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, dict):
        # 우선순위: text/content/decision/title/summary/what/value
        for k in ("text", "content", "decision", "title", "summary", "what", "value", "name", "label", "description"):
            if v.get(k):
                base = _coerce_text(v[k])
                extras = []
                for k2 in ("how", "why", "reason", "rationale", "due", "note"):
                    if v.get(k2):
                        extras.append(f"{k2}={_coerce_text(v[k2])}")
                return base + ((" (" + ", ".join(extras) + ")") if extras else "")
        # 매칭 안 되면 key=value 나열
        try:
            return ", ".join(f"{k}={_coerce_text(vv)}" for k, vv in v.items() if vv)
        except Exception:
            return ""
    if isinstance(v, list):
        return ", ".join(_coerce_text(x) for x in v if x)
    return str(v)


def _normalize(parsed: Dict[str, Any], participants: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    # [r281] 액션아이템 화자 후처리 — '나/본인/우리' 같은 모호한 호칭이 들어왔으면 그대로 두기 어렵지만,
    #   참가자 리스트에 별칭이 매핑되면 정규화. 알 수 없으면 그대로(LLM 출력 존중).
    actions = parsed.get("action_items") or []
    ids = [(p.get("name") or "").strip() for p in (participants or []) if (p.get("name") or "").strip()]
    # 휴리스틱: '나'/'본인' → 액션이 1명만이고 발화 주체가 명확하지 않으니 그대로 둠. (LLM 이 이미 식별자 우선 사용)
    if ids:
        # 부분 일치(별칭) 매핑 — 예: '남규' → 'namkyu7341'. 의도적 정규화 시도.
        def _norm_who(who: str) -> str:
            w = (who or "").strip()
            if not w or w == "모두" or w in ids:
                return w
            wl = w.lower()
            for full in ids:
                fl = full.lower()
                if wl == fl or wl in fl or fl.startswith(wl) or fl in wl:
                    return full
            return w
        actions = [{"who": _norm_who(a.get("who", "")), "what": a.get("what", ""), "due": a.get("due", "")}
                   for a in actions if (a.get("what") or "").strip()]
    # [r292] 새 sections 모델 + principles 보존. 구 모델(topics[].summary) 도 호환.
    raw_topics = parsed.get("topics") or []
    topics = []
    for t in raw_topics:
        if not isinstance(t, dict):
            continue
        # 구 모델 fallback: summary 만 있으면 sections=[{kind:text,content:summary}] 로 승격
        secs = t.get("sections") or []
        if not secs and (t.get("summary") or "").strip():
            secs = [{"kind": "text", "content": t.get("summary")}]
        topics.append({
            "title": t.get("title") or "",
            "lead": t.get("lead") or "",
            "sections": secs,
            # 구 호환: summary 도 유지(있을 경우)
            "summary": t.get("summary") or "",
        })
    # [r295] 견고화 — LLM 이 문자열 배열을 객체로 줘도 텍스트로 강제 변환.
    # [r297] decisions/action what 의 {decision/how/why/근거/기한} 객체는 텍스트 평면화 대신
    #   원본 dict 를 그대로 보존 → to_markdown 이 개조식으로 풀어 더 보기 좋게 렌더.
    agenda = [_coerce_text(a) for a in (parsed.get("agenda") or []) if a]
    principles = [_coerce_text(pr) for pr in (parsed.get("principles") or []) if pr]
    decisions = []
    for d in (parsed.get("decisions") or []):
        if not d: continue
        if isinstance(d, dict):
            # base + 부수 키 분리 보존 — to_markdown 이 들여쓰기로 풀어줌
            base_keys = ("text","content","decision","title","summary","what","value","name","label","description")
            extra_keys = ("how","why","reason","rationale","due","note","근거","방법","왜","어떻게","기한")
            base = ""
            for k in base_keys:
                if d.get(k): base = _coerce_text(d[k]); break
            if not base:
                # 매칭 없으면 평면 텍스트로 폴백
                decisions.append(_coerce_text(d)); continue
            extras = {}
            for k in extra_keys:
                if d.get(k): extras[k] = _coerce_text(d[k])
            decisions.append({"text": base, "extras": extras} if extras else base)
        else:
            decisions.append(_coerce_text(d))
    # action_items 안전화 — what/who/due 가 객체로 와도 풀어서 문자열로
    fixed_actions = []
    for a in actions:
        if isinstance(a, dict):
            fixed_actions.append({
                "who": _coerce_text(a.get("who")),
                "what": _coerce_text(a.get("what")),
                "due": _coerce_text(a.get("due")),
            })
        else:
            fixed_actions.append({"who": "", "what": _coerce_text(a), "due": ""})
    # topics 의 sections 도 같은 처리 — kind 별로 content/items 안전화
    fixed_topics = []
    for t in topics:
        secs_in = t.get("sections") or []
        secs_out = []
        for s in secs_in:
            if not isinstance(s, dict):
                secs_out.append({"kind": "text", "content": _coerce_text(s)})
                continue
            sk = (s.get("kind") or "text").lower()
            ns = {"kind": sk, "heading": _coerce_text(s.get("heading"))}
            if sk == "list":
                ns["items"] = [_coerce_text(x) for x in (s.get("items") or []) if x]
            elif sk == "callout":
                ns["content"] = _coerce_text(s.get("content"))
                ns["by"] = _coerce_text(s.get("by"))
            elif sk == "tier":
                ns["items"] = []
                for it in (s.get("items") or []):
                    if isinstance(it, dict):
                        ns["items"].append({"label": _coerce_text(it.get("label")),
                                            "title": _coerce_text(it.get("title")),
                                            "body": _coerce_text(it.get("body"))})
                    else:
                        ns["items"].append({"label": "", "title": "", "body": _coerce_text(it)})
            elif sk == "table":
                ns["headers"] = [_coerce_text(h) for h in (s.get("headers") or [])]
                ns["rows"] = [[_coerce_text(c) for c in (r or [])] for r in (s.get("rows") or [])]
            else:
                ns["kind"] = "text"
                ns["content"] = _coerce_text(s.get("content"))
            secs_out.append(ns)
        fixed_topics.append({
            "title": _coerce_text(t.get("title")),
            "lead": _coerce_text(t.get("lead")),
            "sections": secs_out,
            "summary": _coerce_text(t.get("summary")),
        })
    return {
        "tldr": _coerce_text(parsed.get("tldr")),
        "agenda": agenda,
        "topics": fixed_topics,
        "decisions": decisions,
        "action_items": fixed_actions,
        "principles": principles,
    }


def _merge_partials(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """[r279] 부분 요약 dict 들을 단순 합치기(중복 제거)."""
    tldr = " ".join([(p.get("tldr") or "").strip() for p in parts if p.get("tldr")]).strip()
    def _uniq(seq):
        out, seen = [], set()
        for x in seq:
            k = json.dumps(x, ensure_ascii=False, sort_keys=True) if isinstance(x, dict) else str(x)
            if k in seen or not k.strip(): continue
            seen.add(k); out.append(x)
        return out
    return {
        "tldr": tldr,
        "agenda": _uniq(sum([p.get("agenda") or [] for p in parts], [])),
        "topics": _uniq(sum([p.get("topics") or [] for p in parts], [])),
        "decisions": _uniq(sum([p.get("decisions") or [] for p in parts], [])),
        "action_items": _uniq(sum([p.get("action_items") or [] for p in parts], [])),
    }


def _partial_or_error(raw: str) -> Dict[str, Any]:
    """[r279] LLM 출력이 JSON 으로 안 떨어졌을 때 — 정규식으로 tldr 만이라도 살리기."""
    raw = (raw or "").strip()
    # 코드펜스 잔재 제거
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"```\s*$", "", raw)
    raw = raw.strip()
    # tldr 키만 정규식으로 추출
    m = re.search(r'"tldr"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    tldr_text = ""
    if m:
        try: tldr_text = json.loads('"' + m.group(1) + '"')
        except Exception: tldr_text = m.group(1)
    if not tldr_text:
        # 그래도 안 되면 raw 앞부분
        tldr_text = raw[:1500]
    return {"tldr": tldr_text, "agenda": [], "topics": [], "decisions": [],
            "action_items": [], "error": "JSON 파싱 실패 — 원문에서 요약 일부만 복구"}


def _fmt_date_kr(iso_str: str) -> str:
    """[r297] ISO → 'YYYY-MM-DD HH:MM' 한국식. 실패 시 원문."""
    try:
        from datetime import datetime
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str


def to_markdown(title: str, summary: Dict[str, Any], started_at: str = "",
                participants: Optional[List[Dict[str, Any]]] = None) -> str:
    """회의록 dict → 위키 내보내기용 마크다운.
    [r292] 새 sections 모델(text/list/callout/tier/table) 렌더.
    [r297] 클로드 HTML 스타일에 가까운 가독성 — 메타 표/안건 chip/개조식 결정·액션.
    """
    p = ", ".join([x.get("name", "") for x in (participants or [])])
    md = [f"# 📝 {title} 회의록", ""]
    # [r297] kicker 한 줄 + 메타 표(클로드 HTML 의 grid 메타와 비슷한 효과)
    md.append("> **PROJECT MEETING · MINUTES**")
    md.append("")
    if started_at or p:
        md.append("| 항목 | 내용 |")
        md.append("|---|---|")
        if started_at:
            md.append(f"| **일시** | {_fmt_date_kr(started_at)} |")
        if p:
            md.append(f"| **참석** | {p} |")
        md.append("")
    if summary.get("tldr"):
        # [r297] tldr 이 너무 길면 문장 단위 줄바꿈으로 호흡 — 가독성 ↑
        tldr = summary["tldr"].replace("다. ", "다.\n").replace("니다. ", "니다.\n")
        md += ["## 요약", tldr, ""]
    if summary.get("agenda"):
        md.append("## 안건")
        for i, a in enumerate(summary["agenda"]):
            md.append(f"- **`{i+1}`** {a}")
        md.append("")
    if summary.get("topics"):
        md.append("## 논의")
        for ti, t in enumerate(summary["topics"]):
            md.append("---")
            md.append("")
            md.append(f"### {ti+1:02d}. {t.get('title','')}")
            if t.get("lead"):
                md += [f"> _{t['lead']}_", ""]
            secs = t.get("sections") or []
            if not secs and t.get("summary"):
                md += [t["summary"], ""]
            for s in secs:
                kind = (s.get("kind") or "text").lower()
                heading = s.get("heading") or ""
                if heading:
                    md.append(f"#### ◆ {heading}")
                    md.append("")
                if kind == "list":
                    for it in (s.get("items") or []):
                        md.append(f"- {it}")
                elif kind == "callout":
                    bb = f" — _{s.get('by')}_" if s.get("by") else ""
                    md += [f"> 💡 **핵심 피드백**{bb}", f"> ",
                           "> " + s.get("content","").replace("\n", "\n> ")]
                elif kind == "tier":
                    for it in (s.get("items") or []):
                        lbl, ttl, body = it.get("label",""), it.get("title",""), it.get("body","")
                        md.append(f"- **`{lbl}` {ttl}** — {body}")
                elif kind == "table":
                    hdrs = s.get("headers") or []
                    rows = s.get("rows") or []
                    if hdrs:
                        md.append("| " + " | ".join(hdrs) + " |")
                        md.append("|" + "|".join(["---"] * len(hdrs)) + "|")
                        for r in rows:
                            md.append("| " + " | ".join([str(c) for c in r]) + " |")
                else:  # text
                    md.append(s.get("content", ""))
                md.append("")
            md.append("")
    if summary.get("decisions"):
        md.append("## ✅ 결정사항")
        md.append("")
        for d in summary["decisions"]:
            if isinstance(d, dict) and d.get("text"):
                md.append(f"- **{d['text']}**")
                for k, v in (d.get("extras") or {}).items():
                    md.append(f"    - _{k}_: {v}")
            else:
                md.append(f"- **{d}**" if isinstance(d, str) else f"- {d}")
        md.append("")
    if summary.get("action_items"):
        md.append("## 📋 액션 아이템")
        md.append("")
        for a in summary["action_items"]:
            who = a.get("who","")
            what = a.get("what","")
            due = a.get("due","")
            due_str = f" · ⏰ `{due}`" if due else ""
            md.append(f"- [ ] **`{who}`** — {what}{due_str}")
        md.append("")
    if summary.get("principles"):
        md.append("## 🎯 합의된 원칙")
        md.append("")
        for pr in summary["principles"]:
            md.append(f"- ✓ {pr}")
        md.append("")
    return "\n".join(md)


def _extract_json(text: str):
    """[r279] 견고한 JSON 추출 — Gemini 의 response_format=json 결과부터
    코드펜스 잔재·꼬리 잘림·닫는 } 누락까지 단계적으로 복구."""
    if not text:
        return None
    s = text.strip()
    # 1) response_format=json 결과면 전체가 그냥 JSON
    if s.startswith("{") and s.endswith("}"):
        try: return json.loads(s)
        except Exception: pass
    # 2) ```json ... ``` 코드펜스 안
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", s)
    if m:
        try: return json.loads(m.group(1))
        except Exception:
            try: return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(1)))
            except Exception: pass
    # 3) 첫 { 부터 마지막 } 까지
    first, last = s.find("{"), s.rfind("}")
    if first < 0:
        return None
    cand = s[first:last + 1] if last > first else s[first:]
    # 시도 a: 그대로
    for raw in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
        try: return json.loads(raw)
        except Exception: pass
    # 4) 닫는 } / ] 누락 자동 보완(스트림 잘림 케이스)
    try:
        return json.loads(_auto_close(cand))
    except Exception:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", _auto_close(cand)))
        except Exception:
            return None


def _auto_close(s: str) -> str:
    """[r279] 따옴표/괄호 누락된 잘린 JSON 끝부분에 닫는 토큰을 추가해 파싱 가능하게 한다.

    문자열 안의 { [ 는 무시(따옴표 인식 + 백슬래시 이스케이프).
    완전한 보장은 아니지만 truncate 된 응답을 살리는 best-effort.
    """
    stack = []           # ['{', '[' ...]
    in_str = False
    esc = False
    for ch in s:
        if esc:
            esc = False; continue
        if ch == '\\' and in_str:
            esc = True; continue
        if ch == '"':
            in_str = not in_str; continue
        if in_str: continue
        if ch in '{[': stack.append(ch)
        elif ch == '}' and stack and stack[-1] == '{': stack.pop()
        elif ch == ']' and stack and stack[-1] == '[': stack.pop()
    tail = ""
    # 문자열 안에서 끝났으면 닫는 따옴표 + 그 다음 trailing comma 잘라내기 우선
    if in_str:
        tail += '"'
    # 마지막 콤마 정리(닫기 직전 trailing comma 방지) — 보수적으로 그대로 두고 ',' 후 ']' / '}' 만 보정
    # 남은 괄호 닫기
    for ch in reversed(stack):
        tail += '}' if ch == '{' else ']'
    return s + tail
