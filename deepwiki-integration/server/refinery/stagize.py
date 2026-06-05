"""[r252] ④-4 Sprint/Task 집계 → STAGE(마일스톤) 정의 (계획 §④-4, 의도 #5).

연속 스프린트를 하나의 제품 목표(STAGE)로 묶는다. 묶음 기준(결정 #4):
  - rule='auto'      : LLM 이 제품목표 전환점으로 자율 그룹 + 명명
  - rule='by_count:N': N 스프린트마다(로직)
  - rule='by_process': 공정 대분류 전환마다(로직)
  - rule='all_one'   : 전체 1 STAGE(로직)
STAGE.tags 는 추측이 아니라 그 STAGE 스프린트들의 Task **공정태그 합집합**을 역산
→ 방향성이 실제 작업에 근거. type/lifecycle/goal 은 LLM 또는 템플릿.
"""
import time
from typing import AsyncIterator, Dict, Any, List, Optional

from llm_router import get_llm
from .intake import _extract_json


# ── 순수 로직(테스트 가능) ──────────────────────────────────
def union_process_tags(sprint_seqs: List[int], sprints: List[Dict[str, Any]], tasks: List[Dict[str, Any]]) -> List[str]:
    """묶인 스프린트들의 Task 공정태그 합집합(등장 순서 보존)."""
    seqset = set(sprint_seqs)
    tids = []
    for s in sprints:
        if s.get("seq") in seqset:
            tids.extend(s.get("task_ids") or [])
    tidset = set(tids)
    out: List[str] = []
    for t in tasks:
        if t.get("id") in tidset:
            pt = t.get("process_tag")
            if pt and pt not in out:
                out.append(pt)
    return out


def _major(tag: str) -> str:
    return (tag or "").split(".")[0]


def group_sprints_by_rule(sprints: List[Dict[str, Any]], tasks: List[Dict[str, Any]], rule: str) -> List[List[int]]:
    """규칙 기반 그룹 → [[sprint seq...], ...]. (rule='auto' 는 여기서 처리 안 함)."""
    seqs = [s.get("seq") for s in sorted(sprints, key=lambda x: x.get("seq", 0))]
    if not seqs:
        return []
    if rule.startswith("by_count:"):
        try:
            n = max(1, int(rule.split(":", 1)[1]))
        except Exception:
            n = 2
        return [seqs[i:i + n] for i in range(0, len(seqs), n)]
    if rule == "by_process":
        groups: List[List[int]] = []
        prev_major = None
        for sq in seqs:
            majors = {_major(t) for t in union_process_tags([sq], sprints, tasks)}
            cur = sorted(majors)[0] if majors else ""
            if prev_major is None or cur == prev_major:
                if not groups:
                    groups.append([])
                groups[-1].append(sq)
            else:
                groups.append([sq])
            prev_major = cur
        return groups
    # all_one (기본 폴백)
    return [seqs]


# ── 메인(SSE) ───────────────────────────────────────────────
_SYSTEM = """당신은 스프린트 묶음을 '제품 마일스톤(STAGE)'으로 정의하는 도구입니다.
주어진 스프린트(각 목표·공정 분포)를 연속 묶음으로 그룹화하고, 각 묶음을 STAGE 로
정의합니다. 한국어, 아래 JSON 만 출력.

[규칙]
1. STAGE 는 연속된 스프린트들의 묶음(제품 목표가 바뀌는 지점에서 분리).
2. type ∈ MVP|PoC|Prototype|Pilot, lifecycle ∈ planning|build|gtm|iterative.
3. name 은 팀이 향할 방향이 보이는 한 줄, goal 은 1~2문장.
4. sprint_seqs 는 입력 스프린트의 seq 정수만.
5. 출력 JSON only.

```json
{ "stages":[{"name":"전투 코어 MVP","type":"MVP","lifecycle":"build","goal":"...","sprint_seqs":[1,2]}] }
```
"""


async def stagize(
    *,
    sprints: List[Dict[str, Any]],
    tasks: List[Dict[str, Any]],
    context: str = "",
    rule: str = "auto",
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """스프린트 → STAGE 정의 (SSE: stage / stage_proposed / done)."""
    yield {"event": "stage", "message": f"스프린트 {len(sprints)}개 → STAGE 집계 (rule={rule})"}

    groups: List[Dict[str, Any]] = []  # {name,type,lifecycle,goal,sprint_seqs}
    if rule == "auto":
        llm = get_llm(model)
        sp_block = "\n".join(
            f"- seq {s.get('seq')}: 공수 {round(s.get('hours',0))}h · 공정 "
            + ",".join(union_process_tags([s.get('seq')], sprints, tasks)[:5])
            for s in sorted(sprints, key=lambda x: x.get("seq", 0))
        )
        prompt = f"[기존 프로젝트]\n{context or '(없음)'}\n\n[스프린트]\n{sp_block}\n\nSTAGE 묶음 JSON 출력."
        buf = ""
        try:
            async for delta in llm.chat_stream(
                messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
                model=model, temperature=0.3,
            ):
                buf += delta
        except Exception as e:
            yield {"event": "warn", "message": f"STAGE LLM 실패: {e} — 규칙(all_one) 폴백"}
        parsed = _extract_json(buf) or {}
        for g in (parsed.get("stages") or []):
            if isinstance(g, dict) and g.get("sprint_seqs"):
                groups.append(g)
    if not groups:
        # 규칙 기반(또는 LLM 실패 폴백)
        rb = group_sprints_by_rule(sprints, tasks, rule if rule != "auto" else "all_one")
        groups = [{"name": "", "type": "MVP", "lifecycle": "build", "goal": "", "sprint_seqs": g} for g in rb]

    stages_out: List[Dict[str, Any]] = []
    for idx, g in enumerate(groups):
        seqs = [int(x) for x in (g.get("sprint_seqs") or []) if str(x).strip().isdigit()]
        if not seqs:
            continue
        tags = union_process_tags(seqs, sprints, tasks)  # 역산(로직)
        item = {
            "id": _uid("stage"),
            "name": (g.get("name") or f"STAGE {idx + 1}")[:80],
            "type": (g.get("type") or "MVP")[:20],
            "lifecycle": (g.get("lifecycle") or "build")[:20],
            "goal": (g.get("goal") or "")[:400],
            "sprint_seqs": seqs,
            "tags": tags,
            "tag_meta": {t: {"goal": "", "deliverables": []} for t in tags},
        }
        stages_out.append(item)
        yield {"event": "stage_proposed", "stage": item}

    yield {"event": "done", "stages": stages_out, "summary": f"STAGE {len(stages_out)}개 (rule={rule})"}


_uid_counter = [0]


def _uid(prefix: str) -> str:
    _uid_counter[0] += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_uid_counter[0]}"
