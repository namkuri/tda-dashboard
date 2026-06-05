"""[r260] ④-2 기술 의존도 추론 — Task 간 '기술적 선후관계'를 LLM 으로 직접 판단.

기존엔 마인드맵 cross_links(개념 관계)를 Task 의존으로 투영하는 근사였다(order.py).
사용자 의도(프롬프트 v2 #3 "기술 의존도")에 맞춰, 실제 Task 목록을 LLM 에 주고
"무엇을 먼저 구현해야 다른 게 가능한가"(기반/데이터/엔진/컴포넌트 선행)를 추론한다.
결과 edge 를 task.depends_on 에 누적(in-place). 순환은 이후 topo_order 가 처리.
"""
import time
from typing import AsyncIterator, Dict, Any, List, Optional

from llm_router import get_llm
from .intake import _extract_json


_SYSTEM = """당신은 게임개발 작업(Task) 사이의 '기술적 선후관계'를 판단하는 도구입니다.
어떤 작업을 먼저 구현해야 다른 작업이 가능한지(기반 시스템·데이터 구조·엔진·컴포넌트가
그 위에 올라가는 기능보다 선행)를 봅니다. 한국어로 판단하고 아래 JSON 만 출력.

[규칙]
1. edges 는 "A(선행)를 먼저 해야 B(후행) 가능" 인 쌍만. from=선행 번호, to=후행 번호.
2. 기반/데이터/엔진/공통 컴포넌트 → 그 위 기능 순으로.
3. 확실한 기술 의존만. 단순 주제 유사·동시 진행 가능 작업엔 edge 금지.
4. 순환 금지(A→B 면 B→A 금지).
5. 출력 JSON only.

```json
{ "edges": [{"from": 1, "to": 3, "reason": "히트박스 컴포넌트가 콤보 판정의 기반"}] }
```
"""


def _task_block(tasks: List[Dict[str, Any]], limit: int = 60) -> str:
    rows = []
    for i, t in enumerate(tasks[:limit]):
        rows.append(f"[{i + 1}] {t.get('title', '')}" + (f" ({t.get('process_tag')})" if t.get('process_tag') else ""))
    return "\n".join(rows)


def apply_edges(tasks: List[Dict[str, Any]], edges: Any) -> int:
    """edges(1-based from/to) → tasks[to].depends_on += tasks[from].id. 검증·중복제거.

    자기참조·범위밖·이미 역방향 존재(즉시 순환) 인 edge 는 건너뛴다.
    """
    n = len(tasks)
    added = 0
    for e in (edges or []):
        if not isinstance(e, dict):
            continue
        try:
            fi = int(e.get("from")) - 1
            ti = int(e.get("to")) - 1
        except Exception:
            continue
        if fi < 0 or ti < 0 or fi >= n or ti >= n or fi == ti:
            continue
        from_id = tasks[fi].get("id")
        to_id = tasks[ti].get("id")
        # 즉시 순환 방지: from 이 이미 to 에 의존하면 skip
        if to_id in (tasks[fi].get("depends_on") or []):
            continue
        dep = tasks[ti].setdefault("depends_on", [])
        if from_id and from_id not in dep:
            dep.append(from_id)
            added += 1
    return added


async def infer_tech_deps(
    *,
    tasks: List[Dict[str, Any]],
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Task 목록 → 기술 의존 edge 추론 후 depends_on 누적(SSE: stage / warn / done)."""
    if len(tasks) < 2:
        yield {"event": "done", "edges": 0}
        return
    llm = get_llm(model)
    yield {"event": "stage", "message": f"Task {len(tasks)}개 기술 의존도 추론"}
    prompt = f"[작업 목록]\n{_task_block(tasks)}\n\n기술적 선후관계 edges JSON 출력."
    buf = ""
    try:
        async for delta in llm.chat_stream(
            messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
            model=model, temperature=0.2,
        ):
            buf += delta
    except Exception as e:
        yield {"event": "warn", "message": f"기술 의존도 LLM 실패: {e} — cross_links 로 폴백"}
    cnt = apply_edges(tasks, (_extract_json(buf) or {}).get("edges"))
    yield {"event": "done", "edges": cnt}
