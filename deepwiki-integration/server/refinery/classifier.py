"""[r223] 분류 추천 (AI) — 노드 묶음 단위 호출.

SSE: stage / batch_done / suggestion / done.
"""
import json
import re
from typing import AsyncIterator, Dict, Any, List, Optional

from ollama_client import get_ollama
from llm_router import get_llm  # [r226]


_SYSTEM = """당신은 연구 노드를 4 분면 중 하나로 추천하는 분류기입니다.

[4 분면]
- canon: 확정된 사실/시스템 — 즉시 구현
- hyp: 가설 — playtest 로 검증 필요
- later: 좋은 아이디어지만 MVP 이후 (백로그)
- cut: 폐기 — 범위 밖, 비현실적, 중복

[규칙]
- 입력 노드 각각에 (category, confidence, reason) 추천
- confidence 0~100. reason 한 문장.
- 출력 JSON only:
```json
{
  "suggestions": [
    {"id": "n_xxx", "category": "canon|hyp|later|cut", "confidence": 85, "reason": "..."}
  ]
}
```
"""


async def classify_suggest(
    *,
    nodes: List[Dict[str, Any]],
    model: Optional[str] = None,
    batch_size: int = 5,
) -> AsyncIterator[Dict[str, Any]]:
    ollama = get_llm(model)  # [r226]
    targets = [n for n in nodes if n.get("kind") != "category"]
    total = len(targets)
    if not total:
        yield {"event": "done", "suggestions": []}
        return
    yield {"event": "stage", "message": f"노드 {total}개 분류 추천 (배치 {batch_size}씩)"}
    all_suggestions: List[Dict[str, Any]] = []
    batches = [targets[i:i + batch_size] for i in range(0, total, batch_size)]
    for bi, batch in enumerate(batches):
        yield {"event": "batch_start", "current": bi + 1, "total": len(batches)}
        listing = "\n".join(
            f"- id={n['id']} title=\"{n['title']}\" kind={n.get('kind','concept')} "
            f"summary=\"{(n.get('summary') or '')[:120]}\""
            for n in batch
        )
        user_prompt = f"노드 {len(batch)}개:\n{listing}\n\nJSON 한 개 출력."
        buf = ""
        try:
            async for delta in ollama.chat_stream(
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                model=model,
                temperature=0.2,
            ):
                buf += delta
        except Exception as e:
            yield {"event": "warn", "message": f"배치 {bi + 1} 실패: {e}"}
            continue
        parsed = _extract_json(buf)
        if not parsed or not isinstance(parsed.get("suggestions"), list):
            continue
        for s in parsed["suggestions"]:
            if not isinstance(s, dict): continue
            cat = (s.get("category") or "later").lower()
            if cat not in ("canon", "hyp", "later", "cut"):
                cat = "later"
            sug = {
                "node_id": s.get("id"),
                "category": cat,
                "confidence": int(s.get("confidence") or 50),
                "reason": (s.get("reason") or "")[:200],
            }
            all_suggestions.append(sug)
            yield {"event": "suggestion", "suggestion": sug}
        yield {"event": "batch_done", "current": bi + 1, "total": len(batches), "percent": round((bi + 1) / len(batches) * 100, 1)}
    yield {"event": "done", "suggestions": all_suggestions, "total": len(all_suggestions)}


def _extract_json(text: str):
    if not text: return None
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    cand = m.group(1) if m else None
    if not cand:
        first = text.find("{"); last = text.rfind("}")
        if first >= 0 and last > first: cand = text[first:last + 1]
    if not cand: return None
    try: return json.loads(cand)
    except Exception:
        try: return json.loads(re.sub(r",\s*([}\]])", r"\1", cand))
        except Exception: return None
