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


async def summarize(segments: List[Dict[str, Any]], *, title: str = "",
                    model: Optional[str] = None,
                    started_at: str = "",
                    participants: Optional[List[Dict[str, Any]]] = None,
                    hint: str = "") -> Dict[str, Any]:
    """세그먼트 → 회의록 dict. LLM 실패 시 빈 구조 + error."""
    if not segments:
        return {"tldr": "", "agenda": [], "topics": [], "decisions": [], "action_items": [],
                "error": "대화내역이 비어있음 — 요약할 내용이 없습니다."}
    # 시각 정보를 포함한 화자: 발화. 짧은 회의도 잘 정리되도록.
    lines = []
    for s in segments:
        t = int(s.get("t", 0)); mm, ss = t // 60, t % 60
        lines.append(f"[{mm:02d}:{ss:02d}] {s.get('speaker','?')}: {s.get('text','')}")
    body = "\n".join(lines)
    if len(body) > 24000:
        body = body[:24000] + "\n…(이하 생략)"
    duration_sec = int(max([(s.get("t", 0) + s.get("dur", 0)) for s in segments], default=0))
    pname = ", ".join([(p.get("name") or "") for p in (participants or [])]) or "(미상)"
    user_parts = [
        f"회의 제목: {title or '(없음)'}",
        f"일시: {started_at or '(미상)'}",
        f"참석: {pname}",
        f"길이: 약 {duration_sec}초 ({duration_sec // 60}분 {duration_sec % 60}초), {len(segments)}문장",
    ]
    if hint:
        user_parts.append(f"추가 메모: {hint}")
    user_parts += ["", "[녹취록]", body, "", "위 회의록을 한국어 JSON 으로 작성하세요."]
    user = "\n".join(user_parts)
    ollama = get_llm(model)
    buf = ""
    try:
        async for delta in ollama.chat_stream(
            messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            model=model, temperature=0.3,
        ):
            buf += delta
    except Exception as e:
        return {"tldr": "", "agenda": [], "topics": [], "decisions": [], "action_items": [],
                "error": f"요약 LLM 실패: {e}"}
    parsed = _extract_json(buf)
    if not parsed:
        return {"tldr": buf.strip()[:1500], "agenda": [], "topics": [], "decisions": [],
                "action_items": [], "error": "JSON 파싱 실패 — 원문 요약으로 대체"}
    # 키 정규화
    return {
        "tldr": parsed.get("tldr") or "",
        "agenda": parsed.get("agenda") or [],
        "topics": parsed.get("topics") or [],
        "decisions": parsed.get("decisions") or [],
        "action_items": parsed.get("action_items") or [],
    }


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
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    cand = m.group(1) if m else None
    if not cand:
        first, last = text.find("{"), text.rfind("}")
        if first >= 0 and last > first:
            cand = text[first:last + 1]
    if not cand:
        return None
    try:
        return json.loads(cand)
    except Exception:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", cand))
        except Exception:
            return None
