"""[r242] 대화내역(세그먼트) → LLM 회의록 요약."""
import json
import re
from typing import List, Dict, Any, Optional

from llm_router import get_llm

_SYSTEM = """당신은 한국어 회의 녹취록을 받아 실무에서 바로 쓸 수 있는 회의록(JSON)으로 정리하는 도구입니다.

[근거 규칙]
1. 녹취록의 내용에만 근거. 없는 사실을 만들지 않는다.
2. 발화의 맥락·뉘앙스를 살려 자연스러운 한국어로 재진술(원문 토씨 그대로가 아니어도 OK).
3. 화자 이름이 있으면 "누가 무엇을 말/제안/결정했는지" 명확히 적는다.
4. STT 오류로 보이는 단어는 자연스러운 표현으로 교정해 적용(예: 분명한 오인식어).

[항목 작성 가이드]
- tldr: 3~5문장. 회의의 목적·핵심 논의·결과(있다면)를 한 단락으로.
- agenda: 회의에서 실제로 다룬 안건/주제 키워드 위주 짧은 항목들.
- topics: 각 안건의 논의 흐름을 2~4문장으로 정리(왜·어떻게·결론/이슈).
- decisions: 합의된 결정. 없으면 빈 배열.
- action_items: 누가(who)·무엇을(what)·언제까지(due, 미상이면 "").

[짧은 회의 처리]
- 분량이 짧아도 발화가 있다면 tldr 만큼은 반드시 채운다.
- 결정/액션이 없으면 빈 배열 OK. 억지로 만들지 말 것.

[출력] 마크다운/설명 없이 JSON only:
```json
{
  "tldr": "3~5문장 핵심 요약",
  "agenda": ["안건1", "..."],
  "topics": [{"title":"주제","summary":"2~4문장 논의 정리"}],
  "decisions": ["결정1", "..."],
  "action_items": [{"who":"담당자","what":"할 일","due":""}]
}
```
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

    common_header = [
        f"회의 제목: {title or '(없음)'}",
        f"일시: {started_at or '(미상)'}",
        f"참석: {pname}",
        f"길이: 약 {duration_sec}초 ({duration_sec // 60}분 {duration_sec % 60}초), {len(segments)}문장",
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
                return _normalize(parsed)
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
        # 리듀스: 부분 요약들을 받아 최종 회의록 한 번 더 LLM
        merged_text = json.dumps(partials, ensure_ascii=False, indent=2)
        user = "\n".join(common_header + [
            f"아래는 회의를 {len(chunks_text)} 부분으로 나눠 정리한 부분 요약(JSON 배열) 입니다. 이를 종합해 회의 전체의 최종 회의록 JSON 한 개를 만드세요(중복 제거·시간 순서 보존).",
            "", "[부분 요약들]", merged_text, "",
            "최종 JSON 만 출력."
        ])
        buf = await _llm_json(llm, _SYSTEM, user, model=model, temperature=0.3, max_out=8000)
        parsed = _extract_json(buf)
        if parsed:
            return _normalize(parsed)
        # 리듀스 실패 — 부분 요약 합치기로 폴백
        return _normalize(_merge_partials(partials))
    except Exception as e:
        msg = str(e)
        if "11434" in msg or "ConnectError" in msg or "Connection refused" in msg or "404 Not Found" in msg:
            err = "Ollama(11434) 미실행 — AI Agent 페이지에서 Gemini 무료 키를 등록하거나, 서버에서 ollama 를 실행하세요."
        else:
            err = f"요약 LLM 실패: {msg[:200]}"
        return {"tldr": "", "agenda": [], "topics": [], "decisions": [], "action_items": [], "error": err}


def _normalize(parsed: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tldr": parsed.get("tldr") or "",
        "agenda": parsed.get("agenda") or [],
        "topics": parsed.get("topics") or [],
        "decisions": parsed.get("decisions") or [],
        "action_items": parsed.get("action_items") or [],
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
