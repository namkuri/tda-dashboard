"""[r242] 대화내역(세그먼트) → LLM 회의록 요약."""
import json
import re
from typing import List, Dict, Any, Optional

from llm_router import get_llm

_SYSTEM = """당신은 한국어 회의 녹취록을 받아 **실무에서 그대로 회람할 수 있는 수준의 회의록(JSON)** 으로 정리하는 도구입니다.
형식이 아니라 **내용의 구체성**이 핵심입니다. 행위(논의했다·설명했다·제시했다·정해졌다·선호했다)만 적지 말고, **무엇을·왜·어떻게·결과는 무엇** 인지를 풀어 쓰세요.

[근거 규칙]
1. 녹취 내용에만 근거. 추측·일반 지식 금지.
2. STT 오인식으로 보이는 단어는 맥락에 맞게 자연스럽게 교정해 사용한다(분명한 경우만).
3. 발화 토씨 그대로보다, 핵심 주장·근거·결론을 재구성해 적는다.

[메타 동사 금지 — 매우 중요]
"논의했다 / 설명했다 / 제시했다 / 선호했다 / 정해졌다 / 결정했다(단독) / 강조했다 / 공유했다 / 검토했다" 같은
"뭐 했는지만 적는 표현"은 그 자체로 사용하지 말고, **실제 알맹이**(주장·근거·수치·결정 사항)를 같이 적는다.
나쁜 예) "환경 요소에 대한 접근 방안을 제시하며, 메커니즘과 구현 방법에 대해 상세히 설명했다."
좋은 예) "ethaniya 제안: 환경을 절차적(procedural) 생성으로 가자. 근거: 프리셋은 베타 후 다양성 확장이 어렵다.
        namkyu7341 반박: 절차적은 NPC 결합 충돌이 잦으니 MVP까지는 프리셋 기반으로. 합의: MVP=프리셋, 베타 후 절차적 확장."

[안건(agenda) 규칙]
- 5~8개로 **통합/묶음**. 비슷한 주제(예: '환경 요소'+'이벤트 요소'+'MVP 계획' → 'MVP 구성과 환경 구현 방안')는 하나로.
- 키워드 단어 1개가 아니라 **이해 가능한 어구**(예: "환경/이벤트 절차적 생성 vs 프리셋")로.
- **중요도 순**으로 정렬(가장 큰 영향·결정이 앞).

[논의(topics) 규칙]
- agenda 와 **1:1 같은 순서·같은 제목**으로 매핑.
- 각 항목 summary 는 3~6문장:
  · 핵심 쟁점/문제 정의
  · 주요 주장(누가 + 무엇을 + 왜)
  · 반대/대안(있으면)
  · 결정·합의 또는 미해결 사항
- "논의됐다/제시됐다" 같은 빈 동사로 끝내지 말 것.

[결정사항(decisions) 규칙]
- 추상명사 단독("의존도 설정", "역할 분담", "테스트 방법 결정") **금지**.
- 항상 **무엇을 + 어떻게 + 왜** 포함.
나쁜 예) "MVP 개발 순서와 의존도가 설정되었습니다."
좋은 예) "MVP 개발 순서를 ① 인벤토리 베스트 버전 설계 → ② 종속 시스템 도출 → ③ 최소 MVP 추출 순으로 결정.
        의존도: 하위 시스템(인벤토리·환경 프리셋)이 상위 시스템(NPC·이벤트)보다 선행."

[액션 아이템(action_items) 규칙]
- who 는 **회의 참가자 정확 식별자**만 사용(예: namkyu7341, ethaniya, wooheesung).
  "나·본인·우리·남규(별칭)" 같은 모호한 호칭은 금지. 발화 맥락에서 자기 자신을 가리키면 발화자 식별자로 대체.
- 같은 담당자의 작업은 한 줄로 묶지 말고, **명확한 단위 액션**으로 분리하되 중복은 통합.
- "전원 모두" 동일 작업이면 who="모두" OK.
- due 미상이면 "" (억지로 채우지 말 것).

[요약(tldr) 규칙]
- 4~6문장 한 단락. "어떤 안건을 다뤘고, 어떤 결정·합의가 있었으며, 누가 무엇을 맡았는지" 한눈에 보이게.
- 메타 동사 단독 금지. 결정/맡은 사람을 명시.

[짧은 회의]
- 발화가 있으면 tldr 만큼은 반드시 채움. 결정·액션이 실제로 없으면 빈 배열 OK(억지 생성 금지).

[출력] 마크다운/설명 없이 JSON only:
```json
{
  "tldr": "4~6문장 핵심 요약",
  "agenda": ["통합된 안건 1 (중요도 순)", "..."],
  "topics": [{"title":"agenda와 동일", "summary":"3~6문장 알맹이 있는 정리"}],
  "decisions": ["무엇을 어떻게(왜) 결정했는지 명확히 — 1문장 1결정"],
  "action_items": [{"who":"namkyu7341|ethaniya|wooheesung|모두","what":"명확한 단위 액션","due":""}]
}
```
"""


_SYSTEM_REDUCE = """당신은 회의를 N개 부분으로 나눠 정리한 부분 회의록(JSON 배열)을 받아,
**전체 회의의 최종 회의록 JSON 1개**로 종합하는 도구입니다. 단순 합치기가 아니라 **재정리**해야 합니다.

[종합 규칙]
1. 같은 주제는 하나의 agenda 항목으로 통합. 결과적으로 안건은 5~8개로.
2. agenda 는 **중요도 순**(영향·결정·시간 비중이 큰 순서)으로 정렬.
3. topics 는 agenda 와 **같은 순서·같은 제목**으로 매핑. summary 는 3~6문장(메타 동사 금지, 알맹이 위주).
4. decisions 는 추상명사 단독 금지. 무엇을·어떻게·왜 포함. 같은 결정은 1개로 통합.
5. action_items 의 who 는 정확한 참가자 식별자(나·본인·별칭 금지). 중복 액션 통합.
6. tldr 은 4~6문장으로 전체 회의의 안건·결정·담당을 압축.

[출력] 시스템 프롬프트의 원본 형식과 동일한 JSON only.
"""


def _segments_to_body(segments):
    """세그먼트 → '[mm:ss] 화자: 발화' 평문."""
    lines = []
    for s in segments:
        t = int(s.get("t", 0)); mm, ss = t // 60, t % 60
        lines.append(f"[{mm:02d}:{ss:02d}] {s.get('speaker','?')}: {s.get('text','')}")
    return "\n".join(lines)


async def _llm_json(llm, system: str, user: str, *, model=None, temperature=0.3, max_out=8000):
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
            buf = await _llm_json(llm, _SYSTEM, user, model=model, temperature=0.3, max_out=8000)
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
        buf = await _llm_json(llm, _SYSTEM_REDUCE, user, model=model, temperature=0.3, max_out=8000)
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
    return {
        "tldr": parsed.get("tldr") or "",
        "agenda": parsed.get("agenda") or [],
        "topics": parsed.get("topics") or [],
        "decisions": parsed.get("decisions") or [],
        "action_items": actions,
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


def to_markdown(title: str, summary: Dict[str, Any], started_at: str = "",
                participants: Optional[List[Dict[str, Any]]] = None) -> str:
    """회의록 dict → 위키 내보내기용 마크다운."""
    p = ", ".join([x.get("name", "") for x in (participants or [])])
    md = [f"# 📝 {title} 회의록", ""]
    if started_at:
        md.append(f"- 일시: {started_at}")
    if p:
        md.append(f"- 참석: {p}")
    md.append("")
    if summary.get("tldr"):
        md += ["## 요약", summary["tldr"], ""]
    if summary.get("agenda"):
        md += ["## 안건"] + [f"- {a}" for a in summary["agenda"]] + [""]
    if summary.get("topics"):
        md.append("## 논의")
        for t in summary["topics"]:
            md.append(f"### {t.get('title','')}")
            md.append(t.get("summary", ""))
            md.append("")
    if summary.get("decisions"):
        md += ["## 결정사항"] + [f"- {d}" for d in summary["decisions"]] + [""]
    if summary.get("action_items"):
        md.append("## 액션 아이템")
        for a in summary["action_items"]:
            due = (" (기한: " + a.get("due", "") + ")") if a.get("due") else ""
            md.append(f"- [ ] **{a.get('who','')}** — {a.get('what','')}{due}")
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
