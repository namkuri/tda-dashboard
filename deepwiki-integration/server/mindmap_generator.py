"""[r196] 마인드맵 자동 생성기.

문서(1개 또는 여러개)를 받아 Ollama로 보내, 중심 노드 + 분기 노드 + 엣지로 구성된
마인드맵 JSON을 생성한다. SSE 진행률 스트리밍.

흐름:
  1) 입력 문서 본문 수집(content 컷)
  2) 단일/다중 모드 분기:
     - 단일: 그 문서 1개의 핵심 개념을 중심+가지로 추출
     - 다중: 공통 개념(중심) + 문서별 클러스터(가지) + 문서 간 관계 추출
  3) LLM 응답 → 마인드맵 JSON 파싱({ central, nodes[], edges[] })
  4) gph diagram payload 형태로 정규화(nodes/edges/parts)

SSE 이벤트:
  {"event": "stage", "stage": "collect"|"compose"|"llm"|"parse"}
  {"event": "progress", "current": i, "total": N, "message": "..."}
  {"event": "done", "diagram": {nodes: [...], edges: [...], central: "..."}}
  {"event": "error", "message": "..."}
"""
import json
import re
from typing import AsyncIterator, Dict, Any, List, Optional

from ollama_client import get_ollama
from wiki_generator import _safe_format  # [r147] brace KeyError 방지


_SINGLE_PROMPT = """당신은 문서를 마인드맵으로 정리하는 전문가입니다.

## 입력 문서
제목: {title}

```
{content}
```

## 작업
이 문서의 핵심 개념을 추출해 **마인드맵** 구조로 정리하세요.

### 규칙
- 중심 노드 1개(문서 주제)
- 1단계 분기 3~7개(주요 카테고리)
- 각 1단계 분기 아래 2단계 분기 2~5개(세부 개념)
- 최대 2단계 깊이까지만
- 노드 제목은 짧고 명확하게(2~10단어). 긴 문장 금지.
- 한국어로 작성

### 출력 형식 — JSON만 출력(설명 텍스트 금지)

```json
{
  "central": "문서 주제 한 줄",
  "branches": [
    {
      "title": "주요 카테고리 1",
      "summary": "1~2문장 요약",
      "children": [
        { "title": "세부 개념 1-1", "summary": "짧은 설명" },
        { "title": "세부 개념 1-2", "summary": "짧은 설명" }
      ]
    }
  ]
}
```
"""


_MULTI_PROMPT = """당신은 여러 문서를 통합 마인드맵으로 정리하는 전문가입니다.

## 입력 문서들 ({doc_count}개)
{doc_summaries}

## 작업
위 문서들의 **공통 주제·핵심 개념·문서 간 관계**를 통합한 **마인드맵**을 만드세요.

### 규칙
- 중심 노드 1개(모든 문서를 아우르는 공통 주제)
- 1단계 분기는 **공통 카테고리**(개념별 묶음). 가능하면 어떤 문서에서 왔는지 origin 배열에 문서 제목 표기.
- 2단계 분기는 카테고리 안의 세부 개념(또는 특정 문서의 고유 개념)
- 문서 간 관계가 있으면 cross_links에 기록(예: 문서A의 X가 문서B의 Y와 연결)
- 한국어로 작성. 노드 제목은 짧게(2~10단어).

### 출력 형식 — JSON만(설명 금지)

```json
{
  "central": "통합 주제 한 줄",
  "branches": [
    {
      "title": "공통 카테고리",
      "summary": "이 카테고리 요약",
      "origin": ["문서A 제목", "문서B 제목"],
      "children": [
        { "title": "세부 개념", "summary": "...", "origin": ["문서A 제목"] }
      ]
    }
  ],
  "cross_links": [
    { "from": "노드 제목 1", "to": "노드 제목 2", "label": "관계 설명" }
  ]
}
```
"""


_PARTS = ['ai', 'planning', 'shared', 'scenario', 'terrain', 'extra1', 'extra2']


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """LLM 응답에서 첫 JSON 객체 추출(```json ...``` 블록 또는 raw)."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    candidate = m.group(1) if m else None
    if not candidate:
        # 첫 { ~ 마지막 } 사이를 시도
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            candidate = text[first:last + 1]
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except Exception:
        # 흔한 깨짐 보정 — trailing comma 제거 시도
        try:
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
            return json.loads(fixed)
        except Exception:
            return None


def _layout_radial(central_title: str, branches: List[Dict[str, Any]]) -> Dict[str, Any]:
    """마인드맵 트리 → gph diagram payload(nodes, edges, parts) 정규화 + 방사형 좌표 부여."""
    import math
    import time

    uid_counter = [0]

    def uid(prefix: str) -> str:
        uid_counter[0] += 1
        return f"{prefix}_{int(time.time() * 1000)}_{uid_counter[0]}"

    nodes = []
    edges = []

    # 중심 — 마인드맵 알약 크기 (프론트가 텍스트 기반으로 폭 재계산하지만 안전한 초기값)
    central_id = uid("n")
    nodes.append({
        "id": central_id, "x": 0, "y": 0, "w": 200, "h": 36,
        "part": "shared", "title": (central_title or "마인드맵")[:80],
        "meta": "", "body": "", "highlighted": True,
    })

    # 1단계 — 방사형 배치
    n1 = max(1, len(branches))
    R1 = 360
    for i, br in enumerate(branches):
        angle = (2 * math.pi * i) / n1 - math.pi / 2
        bx = math.cos(angle) * R1
        by = math.sin(angle) * R1
        part = _PARTS[i % len(_PARTS)]
        b_id = uid("n")
        origin = (br.get("origin") or [])
        meta_str = " · ".join(origin[:2]) if origin else ""
        nodes.append({
            "id": b_id, "x": bx, "y": by, "w": 180, "h": 36,
            "part": part, "title": (br.get("title") or "")[:80],
            "meta": meta_str[:80], "body": (br.get("summary") or "")[:240],
            "highlighted": False,
        })
        edges.append({
            "id": uid("e"), "from": central_id, "to": b_id,
            "label": "", "style": "curve",
        })

        # 2단계 — 1단계 노드 주변에 부채꼴로
        children = br.get("children") or []
        n2 = max(1, len(children))
        R2 = 220
        spread = math.pi / 1.8  # 부채각
        base_angle = angle
        for j, ch in enumerate(children):
            t = (j - (n2 - 1) / 2) / max(1, n2 - 1) if n2 > 1 else 0
            cangle = base_angle + t * spread
            cx = bx + math.cos(cangle) * R2
            cy = by + math.sin(cangle) * R2
            c_origin = (ch.get("origin") or [])
            c_meta = " · ".join(c_origin[:2]) if c_origin else ""
            c_id = uid("n")
            nodes.append({
                "id": c_id, "x": cx, "y": cy, "w": 160, "h": 36,
                "part": part, "title": (ch.get("title") or "")[:80],
                "meta": c_meta[:80], "body": (ch.get("summary") or "")[:200],
                "highlighted": False,
            })
            edges.append({
                "id": uid("e"), "from": b_id, "to": c_id,
                "label": "", "style": "curve",
            })

    return {
        "mode": "mindmap",   # [r197] 그래프 1개에 흐름/그래프/마인드맵 중 하나 — 마인드맵 모드로 명시
        "orientation": "horizontal",
        "nodes": nodes,
        "edges": edges,
        "points": [],
        "memos": [],
        "parts": [],
        "description": "🧠 LLM이 생성한 마인드맵",
        "central_id": central_id,
    }


def _add_cross_links(payload: Dict[str, Any], cross_links: List[Dict[str, Any]]) -> None:
    """cross_links의 노드 제목을 id로 매핑해 엣지 추가(다중 모드)."""
    import time
    title_to_id = {}
    for n in payload["nodes"]:
        title_to_id.setdefault(n["title"], n["id"])
    for cl in cross_links or []:
        fid = title_to_id.get((cl.get("from") or "").strip())
        tid = title_to_id.get((cl.get("to") or "").strip())
        if not fid or not tid or fid == tid:
            continue
        payload["edges"].append({
            "id": f"e_cross_{int(time.time()*1000)}_{len(payload['edges'])}",
            "from": fid, "to": tid,
            "label": (cl.get("label") or "")[:40],
            "style": "dashed",
        })


async def generate_mindmap(
    docs: List[Dict[str, Any]],
    model: Optional[str] = None,
    mode: str = "auto",
) -> AsyncIterator[Dict[str, Any]]:
    """마인드맵 생성 SSE 스트림.

    docs: [{ "title": "...", "content": "..." }, ...]
    mode: 'auto'(단일이면 single, 다중이면 multi) | 'single' | 'multi'
    """
    if not docs:
        yield {"event": "error", "message": "문서가 없습니다."}
        return

    ollama = get_ollama()
    if not await ollama.ping():
        yield {"event": "error", "message": "Ollama 연결 실패. GPU 호스트가 가동 중인지 확인하세요."}
        return

    yield {"event": "stage", "stage": "collect", "message": f"{len(docs)}개 문서 수집됨"}

    # 본문 컷(단일 모드는 넉넉히, 다중은 빠르게)
    is_multi = (mode == "multi") or (mode == "auto" and len(docs) > 1)

    if is_multi:
        # 다중 — 문서별 요약 블록
        blocks = []
        for d in docs[:8]:  # 최대 8개
            title = (d.get("title") or "").strip() or "(제목 없음)"
            content = (d.get("content") or "")[:3000]
            blocks.append(f"### 📄 {title}\n\n{content}")
        doc_summaries = "\n\n---\n\n".join(blocks)
        prompt = _safe_format(_MULTI_PROMPT, doc_count=len(docs), doc_summaries=doc_summaries)
        yield {"event": "stage", "stage": "llm", "message": "LLM에 통합 마인드맵 요청 중..."}
    else:
        d = docs[0]
        title = (d.get("title") or "").strip() or "(제목 없음)"
        content = (d.get("content") or "")[:9000]
        prompt = _safe_format(_SINGLE_PROMPT, title=title, content=content)
        yield {"event": "stage", "stage": "llm", "message": f"LLM에 단일 마인드맵 요청 중... ({title})"}

    # LLM 호출
    chunks: List[str] = []
    try:
        async for delta in ollama.chat_stream(
            messages=[
                {"role": "system", "content": "당신은 문서를 마인드맵 JSON으로 정리하는 도우미입니다. 항상 유효한 JSON만 출력합니다."},
                {"role": "user", "content": prompt},
            ],
            model=model,
            temperature=0.3,
        ):
            chunks.append(delta)
            # 100자마다 진행 알림
            if len(chunks) % 50 == 0:
                yield {"event": "progress", "current": len("".join(chunks)), "total": 0, "message": "LLM 응답 수신 중..."}
    except Exception as e:
        yield {"event": "error", "message": f"LLM 호출 실패: {type(e).__name__}: {e}"}
        return

    raw = "".join(chunks).strip()
    yield {"event": "stage", "stage": "parse", "message": "응답 파싱 중..."}

    parsed = _extract_json(raw)
    if not parsed or not isinstance(parsed, dict):
        yield {"event": "error", "message": "LLM 응답에서 유효한 JSON을 찾지 못했습니다.", "raw_head": raw[:300]}
        return

    central = (parsed.get("central") or "마인드맵").strip()
    branches = parsed.get("branches") or []
    if not isinstance(branches, list) or not branches:
        yield {"event": "error", "message": "분기(branches)가 비어 있습니다.", "raw_head": raw[:300]}
        return

    payload = _layout_radial(central, branches)

    # 다중 모드: cross_links 처리
    if is_multi:
        cross = parsed.get("cross_links") or []
        if isinstance(cross, list):
            _add_cross_links(payload, cross)

    yield {
        "event": "done",
        "diagram": payload,
        "central": central,
        "node_count": len(payload["nodes"]),
        "edge_count": len(payload["edges"]),
        "mode": "multi" if is_multi else "single",
    }
