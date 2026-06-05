"""[r242] 대화내역(세그먼트) → LLM 회의록 요약."""
import json
import re
from typing import List, Dict, Any, Optional

from llm_router import get_llm

_SYSTEM = """당신은 회의 녹취록을 받아 한국어 회의록(JSON)으로 정리하는 도구입니다.

[규칙]
1. 녹취 내용에만 근거(없는 사실 금지).
2. 화자 이름을 활용해 누가 무엇을 말/결정했는지 명확히.
3. 액션아이템은 담당자(who)·할 일(what)·기한(due, 없으면 "")으로.
4. 출력은 JSON only:
```json
{
  "tldr": "3~5문장 핵심 요약",
  "agenda": ["논의된 안건1", "..."],
  "topics": [{"title":"주제","summary":"2~4문장 논의 정리"}],
  "decisions": ["결정사항1", "..."],
  "action_items": [{"who":"담당자","what":"할 일","due":""}]
}
```
"""


async def summarize(segments: List[Dict[str, Any]], *, title: str = "",
                    model: Optional[str] = None) -> Dict[str, Any]:
    """세그먼트 → 회의록 dict. LLM 실패 시 빈 구조 + error."""
    # 너무 길면 앞부분 위주로(토큰 보호) — 화자: 발화 평문
    lines = []
    for s in segments:
        lines.append(f"{s.get('speaker','?')}: {s.get('text','')}")
    body = "\n".join(lines)
    if len(body) > 24000:
        body = body[:24000] + "\n…(이하 생략)"
    user = f"회의 제목: {title or '(없음)'}\n\n[녹취록]\n{body}\n\n위 회의록 JSON 작성."
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
