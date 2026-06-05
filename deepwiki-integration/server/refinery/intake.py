"""[r247] 들여오기 추천 — 분해 구조를 훑어 "이 문서를 프로젝트로 어떻게 들여올지" 한 번에 제안.

분류(canon/hyp/later/cut) 대신, 분해 단계에서 만든 구조(카테고리 트리 + 키워드 잎)를
그대로 LLM 에 주고 한 번의 호출로 프로젝트 들여오기 계획을 받는다:

  - stages  : 프로덕트 관리 STAGE(개발 목표) 정의 추천
  - wbs      : WBS 그룹 + 하위 태스크(4~8h)
  - sprints  : 신규 스프린트(주차 목표) 추천
  - issues   : 가설/리스크 → 이슈
  - wiki     : 프로젝트 위키 문서 '구조(목차)'만 — 본문은 후속 단계(별도)

결과는 한 화면에서 채택 → 생성(STAGE=product_stages, 스프린트=sprints,
WBS/태스크/이슈=기존 apply_work). wiki 는 다음 '위키 구조' 단계로 이월.
"""
import json
import re
import time
from typing import AsyncIterator, Dict, Any, List, Optional

from llm_router import get_llm  # gemini-* → Gemini, else Ollama


_SYSTEM = """당신은 기획/연구 문서를 게임개발 프로젝트로 '들여오는' 방법을 설계하는 도구입니다.
주어진 문서 구조(카테고리 트리 + 키워드)를 훑어, 각 내용을 프로젝트의 어디에 넣으면 좋을지
'들여오기 계획'을 한 번에 제안합니다. 한국어로 작성하고, 아래 JSON 만 출력합니다.

[배치 기준]
- STAGE(stages): 큰 개발 목표/제품 단계. type 은 MVP|PoC|Prototype|Pilot 중 하나. goal 은 1~2문장.
- WBS(wbs): 비슷한 노드 3~7개를 묶은 작업 그룹. 하위 tasks 는 4~8시간 단위.
- 스프린트(sprints): 1~2주 안에 끝낼 묶음의 주차 목표(week_label 예: "2026 W23").
- 이슈(issues): 검증이 필요한 가설/리스크. target 은 playtest|defense|tech 중 하나.
- 위키(wiki): 프로젝트 위키로 정리하면 좋은 문서의 '구조(목차)'만. 본문은 쓰지 말 것.
  각 항목은 title + summary(1문장) + outline(목차 문자열 배열, 3~8개).

[규칙]
1. 모든 배열은 비어 있어도 됨(해당 없으면 []). 억지로 채우지 말 것.
2. wbs.node_ids 는 입력 구조의 노드 id 를 참조(없으면 []).
3. 출력은 아래 JSON 객체 하나. 코드블록(```json) 안에 담아도 됨.

```json
{
  "stages": [{"name":"...", "type":"MVP", "goal":"...", "lifecycle":"개발"}],
  "wbs": [{"title":"...", "description":"...", "node_ids":["n_x"], "estimated_days":2,
            "depends_on":[], "tasks":[{"title":"...", "description":"...", "priority":"p1", "hours":6}]}],
  "sprints": [{"week_label":"2026 W23", "goal":"..."}],
  "issues": [{"title":"...", "description":"...", "priority":"p1", "target":"playtest"}],
  "wiki": [{"title":"...", "summary":"...", "outline":["개요", "상세", "..."]}]
}
```
"""


def _structure_outline(nodes: List[Dict[str, Any]], limit: int = 120) -> str:
    """분해 노드 → LLM 입력용 들여쓰기 트리 텍스트(카테고리 > 키워드 잎)."""
    by_parent: Dict[Any, List[Dict[str, Any]]] = {}
    for n in nodes:
        by_parent.setdefault(n.get("parent_id"), []).append(n)
    lines: List[str] = []

    def walk(parent, depth):
        for n in by_parent.get(parent, []):
            if len(lines) >= limit:
                return
            pad = "  " * depth
            mark = "▸" if n.get("kind") == "category" else "-"
            summ = (n.get("summary") or "").strip().replace("\n", " ")
            tail = f" — {summ[:80]}" if summ else ""
            lines.append(f"{pad}{mark} [{n.get('id')}] {n.get('title','')}{tail}")
            walk(n.get("id"), depth + 1)

    walk(None, 0)
    return "\n".join(lines)


async def propose_intake(
    *,
    nodes: List[Dict[str, Any]],
    vault_docs: Optional[List[Dict[str, Any]]] = None,
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """분해 구조 → 들여오기 계획(STAGE/WBS+태스크/스프린트/이슈/위키구조).

    SSE 이벤트: stage / wbs_proposed / stage_proposed / sprint_proposed /
               issue_proposed / wiki_proposed / done(전체 목록 포함)
    """
    llm = get_llm(model)
    outline = _structure_outline(nodes or [])
    cat_cnt = sum(1 for n in (nodes or []) if n.get("kind") == "category")
    leaf_cnt = len(nodes or []) - cat_cnt
    yield {"event": "stage", "message": f"구조 {cat_cnt}분류 · {leaf_cnt}키워드 → 들여오기 계획 요청"}

    titles = ", ".join((d.get("title") or "") for d in (vault_docs or [])[:8])
    user_prompt = (
        f"문서: {titles}\n\n[분해 구조]\n{outline}\n\n"
        "위 구조를 프로젝트로 들여오는 계획 JSON 을 출력하세요."
    )

    buf = ""
    try:
        async for delta in llm.chat_stream(
            messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user_prompt}],
            model=model, temperature=0.3,
        ):
            buf += delta
            if len(buf) % 400 < len(delta):  # 대략 주기적 진행 알림
                yield {"event": "stage", "message": f"LLM 응답 {len(buf)}자 수신…"}
    except Exception as e:
        yield {"event": "warn", "message": f"들여오기 LLM 실패: {e}"}

    parsed = _extract_json(buf) or {}

    stages: List[Dict[str, Any]] = []
    for s in (parsed.get("stages") or []):
        if not isinstance(s, dict):
            continue
        item = {
            "id": _uid("stage_proposed"),
            "name": (s.get("name") or "새 Stage")[:80],
            "type": (s.get("type") or "MVP")[:20],
            "goal": (s.get("goal") or "")[:400],
            "lifecycle": (s.get("lifecycle") or "")[:40],
        }
        stages.append(item)
        yield {"event": "stage_proposed", "stage": item}

    wbs_nodes: List[Dict[str, Any]] = []
    tasks: List[Dict[str, Any]] = []
    for w in (parsed.get("wbs") or []):
        if not isinstance(w, dict):
            continue
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
        for t in (w.get("tasks") or []):
            if not isinstance(t, dict):
                continue
            tasks.append({
                "id": _uid("task_proposed"),
                "title": (t.get("title") or "태스크")[:80],
                "description": (t.get("description") or "")[:400],
                "priority": (t.get("priority") or "p2").lower(),
                "hours": float(t.get("hours") or 4),
                "wbs_id": wid,
                "kind": "task_proposed",
            })

    sprints: List[Dict[str, Any]] = []
    for sp in (parsed.get("sprints") or []):
        if not isinstance(sp, dict):
            continue
        item = {
            "id": _uid("sprint_proposed"),
            "week_label": (sp.get("week_label") or sp.get("weekLabel") or "스프린트")[:40],
            "goal": (sp.get("goal") or "")[:400],
        }
        sprints.append(item)
        yield {"event": "sprint_proposed", "sprint": item}

    issues: List[Dict[str, Any]] = []
    for it in (parsed.get("issues") or []):
        if not isinstance(it, dict):
            continue
        item = {
            "id": _uid("issue_proposed"),
            "title": (it.get("title") or "이슈")[:120],
            "description": (it.get("description") or "")[:400],
            "priority": (it.get("priority") or "p2").lower(),
            "status": "open",
            "target": (it.get("target") or "playtest")[:20],
            "kind": "issue_proposed",
        }
        issues.append(item)
        yield {"event": "issue_proposed", "issue": item}

    wiki: List[Dict[str, Any]] = []
    for wk in (parsed.get("wiki") or []):
        if not isinstance(wk, dict):
            continue
        outline_list = [str(x)[:120] for x in (wk.get("outline") or []) if str(x).strip()]
        item = {
            "id": _uid("wiki_proposed"),
            "title": (wk.get("title") or "문서")[:120],
            "summary": (wk.get("summary") or "")[:300],
            "outline": outline_list[:12],
        }
        wiki.append(item)
        yield {"event": "wiki_proposed", "wiki": item}

    summary = (
        f"STAGE {len(stages)} · WBS {len(wbs_nodes)} · 태스크 {len(tasks)} · "
        f"스프린트 {len(sprints)} · 이슈 {len(issues)} · 위키 {len(wiki)}"
    )
    if not (stages or wbs_nodes or tasks or sprints or issues or wiki):
        yield {"event": "warn", "message": "LLM 결과가 비었습니다 — 모델을 바꾸거나 다시 시도하세요."}
    yield {
        "event": "done",
        "stages": stages,
        "wbs_nodes": wbs_nodes,
        "tasks": tasks,
        "sprints": sprints,
        "issues": issues,
        "wiki": wiki,
        "summary": summary,
    }


def _extract_json(text: str):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    cand = m.group(1) if m else None
    if not cand:
        first = text.find("{")
        last = text.rfind("}")
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


_uid_counter = [0]


def _uid(prefix: str) -> str:
    _uid_counter[0] += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_uid_counter[0]}"
