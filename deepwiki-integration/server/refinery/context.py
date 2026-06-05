"""[r250] 프로젝트 컨텍스트 빌더 (계획 §5, 의도 #10).

④ 도출 단계의 모든 LLM 호출에 '기존 프로젝트 상태'를 압축 주입한다.
→ 중복 작업 생략, 기존 STAGE/WBS 확장 vs 신규 판단, 적합 위치 배치.

state 는 프론트가 동봉(현재 currentData + PM_TAX). 백엔드 중복 정의를 피하려고
태그 분류(pm_tax)도 프론트에서 받는다. 모든 항목은 선택(없으면 생략).
state = {
  stages:    [{name, type, lifecycle, tags:[...]}],
  wbs:       [{title, parent_title?}],
  sprints:   [{week_label, status}],
  categories:[{title}],
  pm_tax:    [{i, s, l}],     # 공정/직무 태그(소분류 위주)
}
"""
from typing import List, Dict, Any, Optional


def _line(items: List[str], sep: str = " · ", limit: int = 40) -> str:
    items = [x for x in items if x]
    if len(items) > limit:
        items = items[:limit] + [f"…(+{len(items) - limit})"]
    return sep.join(items)


def build_project_context(state: Optional[Dict[str, Any]]) -> str:
    """프로젝트 상태 → LLM 프롬프트용 압축 텍스트 블록."""
    state = state or {}
    out: List[str] = []

    stages = state.get("stages") or []
    if stages:
        rows = []
        for s in stages[:30]:
            tags = ",".join(str(t) for t in (s.get("tags") or [])[:6])
            meta = "/".join(x for x in [s.get("type"), s.get("lifecycle")] if x)
            rows.append(f"{s.get('name','(STAGE)')}" + (f"({meta})" if meta else "") + (f" tags:{tags}" if tags else ""))
        out.append("[기존 STAGE] " + _line(rows))

    wbs = state.get("wbs") or []
    if wbs:
        rows = []
        for w in wbs[:50]:
            t = w.get("title") or "(WBS)"
            p = w.get("parent_title")
            rows.append(f"{p} > {t}" if p else t)
        out.append("[기존 WBS] " + _line(rows))

    sprints = state.get("sprints") or []
    if sprints:
        rows = [f"{s.get('week_label') or s.get('weekLabel') or '(스프린트)'}({s.get('status','')})" for s in sprints[:20]]
        out.append("[기존 Sprint] " + _line(rows))

    cats = state.get("categories") or []
    if cats:
        out.append("[칸반 카테고리] " + _line([c.get("title") or "" for c in cats[:30]]))

    pm = state.get("pm_tax") or []
    if pm:
        # 소분류(v==3) 위주, 없으면 전체. id·축약(전체) 형태.
        leaves = [t for t in pm if str(t.get("v")) == "3"] or pm
        rows = [f"{t.get('i')}:{t.get('s') or t.get('l') or ''}" for t in leaves[:70]]
        out.append("[공정 태그(PM_TAX)] " + _line(rows, sep=", ", limit=70))

    if not out:
        return "(기존 프로젝트 데이터 없음 — 신규 프로젝트로 간주)"
    return "\n".join(out)
