"""[r223] WBS / 태스크 / 이슈 자동 제안.

확정 노드 → WBS 그룹핑(3~7노드 1그룹) → 4~8시간 단위 태스크 분할.
가설 노드 → 이슈 (검증 인프라).
리스크 노드 → 이슈 (방어 항목).
"""
import json
import re
import time
from typing import AsyncIterator, Dict, Any, List, Optional

from ollama_client import get_ollama


_SYSTEM_WBS = """확정 노드들을 작업구조화(WBS) 그룹으로 묶는 도구입니다.

[규칙]
1. 비슷한 시스템·관계 깊은 노드 3~7개를 1 WBS 노드로 묶음.
2. WBS 노드는 title(1~5단어) + description(1~3문장) + node_ids[] + estimated_days(0.5~5).
3. 의존성 — 다른 WBS 의 id 참조 가능 (depends_on[]).
4. 출력 JSON only:
```json
{
  "wbs_nodes": [
    {"title":"...", "description":"...", "node_ids":["n_xxx"], "estimated_days":2, "depends_on":[]}
  ]
}
```
"""


_SYSTEM_TASKS = """WBS 노드 1개를 4~8시간 단위 태스크로 분할합니다.

[규칙]
1. 태스크당 4~8시간. 미만이면 묶고 초과면 분할.
2. 태스크는 title + description + priority(p0|p1|p2|p3).
3. 출력 JSON only:
```json
{
  "tasks": [
    {"title":"...", "description":"...", "priority":"p1", "hours":6}
  ]
}
```
"""


async def propose_work(
    *,
    nodes: List[Dict[str, Any]],
    classifications: Dict[str, str],
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """확정/가설/리스크 노드 → WBS/태스크/이슈 제안.

    SSE 이벤트:
      stage / wbs_proposed / tasks_proposed / issue_proposed / done
    """
    ollama = get_ollama()
    canon_nodes = [n for n in nodes if classifications.get(n["id"]) == "canon" and n.get("kind") != "category"]
    hyp_nodes = [n for n in nodes if classifications.get(n["id"]) == "hyp"]
    risk_nodes = [n for n in nodes if (n.get("kind") == "risk")]

    yield {"event": "stage", "message": f"확정 {len(canon_nodes)} · 가설 {len(hyp_nodes)} · 리스크 {len(risk_nodes)}"}

    # ── WBS 제안
    wbs_nodes: List[Dict[str, Any]] = []
    if canon_nodes:
        listing = "\n".join(
            f"- {n['id']}: {n['title']} — {(n.get('summary') or '')[:120]}" for n in canon_nodes[:80]
        )
        user_prompt = f"확정 노드 {len(canon_nodes)}개:\n{listing}\n\nWBS 그룹화 JSON."
        buf = ""
        try:
            async for delta in ollama.chat_stream(
                messages=[{"role": "system", "content": _SYSTEM_WBS}, {"role": "user", "content": user_prompt}],
                model=model, temperature=0.3,
            ):
                buf += delta
        except Exception as e:
            yield {"event": "warn", "message": f"WBS LLM 실패: {e}"}
        parsed = _extract_json(buf)
        if parsed and isinstance(parsed.get("wbs_nodes"), list):
            for w in parsed["wbs_nodes"]:
                if not isinstance(w, dict): continue
                wid = _uid("wbs_proposed")
                wnode = {
                    "id": wid,
                    "title": (w.get("title") or "WBS")[:60],
                    "description": (w.get("description") or "")[:400],
                    "node_ids": w.get("node_ids") or [],
                    "estimated_days": float(w.get("estimated_days") or 1.0),
                    "depends_on": w.get("depends_on") or [],
                    "kind": "wbs_proposed",
                }
                wbs_nodes.append(wnode)
                yield {"event": "wbs_proposed", "wbs": wnode}

    # ── 태스크 분할 (WBS 별)
    all_tasks: List[Dict[str, Any]] = []
    for wb in wbs_nodes:
        # 해당 WBS 의 노드 정보 조회
        included = [n for n in canon_nodes if n["id"] in (wb.get("node_ids") or [])]
        if not included: continue
        node_summary = "\n".join(f"- {n['title']}: {(n.get('summary') or '')[:80]}" for n in included)
        user_prompt = f"WBS: {wb['title']} ({wb['description']})\n\n포함 노드:\n{node_summary}\n\n태스크 JSON."
        buf = ""
        try:
            async for delta in ollama.chat_stream(
                messages=[{"role": "system", "content": _SYSTEM_TASKS}, {"role": "user", "content": user_prompt}],
                model=model, temperature=0.3,
            ):
                buf += delta
        except Exception as e:
            yield {"event": "warn", "message": f"태스크 LLM 실패 ({wb['title']}): {e}"}
            continue
        parsed = _extract_json(buf)
        if parsed and isinstance(parsed.get("tasks"), list):
            for t in parsed["tasks"]:
                if not isinstance(t, dict): continue
                task = {
                    "id": _uid("task_proposed"),
                    "title": (t.get("title") or "태스크")[:80],
                    "description": (t.get("description") or "")[:400],
                    "priority": (t.get("priority") or "p2").lower(),
                    "hours": float(t.get("hours") or 4),
                    "wbs_id": wb["id"],
                    "kind": "task_proposed",
                }
                all_tasks.append(task)
            yield {"event": "tasks_proposed", "wbs_id": wb["id"], "tasks_count": len([t for t in all_tasks if t["wbs_id"] == wb["id"]])}

    # ── 이슈 (가설 + 리스크)
    issues: List[Dict[str, Any]] = []
    for h in hyp_nodes:
        issue = {
            "id": _uid("issue_proposed"),
            "title": f"[가설] {h['title']}",
            "description": h.get("summary", "")[:400],
            "priority": "p1",
            "status": "open",
            "node_id": h["id"],
            "target": "playtest",
            "kind": "issue_proposed",
        }
        issues.append(issue)
        yield {"event": "issue_proposed", "issue": issue}
    for r in risk_nodes:
        issue = {
            "id": _uid("issue_proposed"),
            "title": f"[리스크] {r['title']}",
            "description": r.get("summary", "")[:400],
            "priority": "p0",
            "status": "open",
            "node_id": r["id"],
            "target": "defense",
            "kind": "issue_proposed",
        }
        issues.append(issue)
        yield {"event": "issue_proposed", "issue": issue}

    yield {
        "event": "done",
        "wbs_nodes": wbs_nodes,
        "tasks": all_tasks,
        "issues": issues,
        "summary": f"WBS {len(wbs_nodes)} · 태스크 {len(all_tasks)} · 이슈 {len(issues)}",
    }


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


_uid_counter = [0]
def _uid(prefix: str) -> str:
    _uid_counter[0] += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_uid_counter[0]}"
