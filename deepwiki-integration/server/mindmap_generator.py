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
이 문서의 모든 의미 있는 개념을 추출해 **풍부한 마인드맵**으로 정리하세요. 핵심만 뽑지 말고
문서가 다루는 거의 모든 주제를 포함하세요.

### 규칙 (적게 만들지 마세요. 문서가 길수록 더 많이.)
- 중심 노드 1개(문서 주제 한 줄)
- 1단계 분기 **5~12개**(주요 카테고리·섹션·축)
- 각 1단계 분기 아래 2단계 분기 **3~8개**(개념·하위 항목)
- 각 2단계 분기 아래 3단계 분기 **2~6개**(세부·예시·근거) — 필요한 곳에만 추가
- 깊이는 최대 **4단계**까지 가능(꼭 필요한 가지에만)
- 노드 제목은 짧고 명확하게(2~12단어). 본문 문장 그대로 옮기지 말고 요약된 라벨로.
- 가능한 한 문서의 표·리스트·소제목·예시·반대 의견까지 노드로 추출
- 한국어로 작성(원문이 영어면 영어 라벨 유지 OK)

### 출력 형식 — JSON만 출력(설명 텍스트 금지)

```json
{
  "central": "문서 주제 한 줄",
  "branches": [
    {
      "title": "주요 카테고리 1",
      "summary": "1~2문장 요약",
      "children": [
        {
          "title": "하위 개념 1-1",
          "summary": "짧은 설명",
          "children": [
            { "title": "세부 1-1-1", "summary": "..." },
            { "title": "세부 1-1-2", "summary": "..." }
          ]
        },
        { "title": "하위 개념 1-2", "summary": "짧은 설명" }
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
위 문서들의 **공통 주제·핵심 개념·문서 간 관계**를 통합한 **풍부한 마인드맵**을 만드세요.
각 문서의 핵심을 모두 포함하면서 공통 구조를 찾으세요.

### 규칙 (적게 만들지 마세요)
- 중심 노드 1개(모든 문서를 아우르는 공통 주제)
- 1단계 분기 **6~14개**: 공통 카테고리(개념별 묶음). origin 배열에 어느 문서들에서 나왔는지 표기.
- 2단계 분기 **3~7개**: 카테고리 안의 세부 개념(또는 특정 문서 고유 개념)
- 3단계 분기 **2~5개**: 추가 세부(필요한 곳만)
- 깊이 최대 **4단계**까지
- 문서 간 관계가 있으면 cross_links에 기록(예: 문서A의 X가 문서B의 Y와 연결)
- 한국어로 작성. 노드 제목은 짧게(2~12단어).

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
    """마인드맵 트리 → gph diagram payload(nodes, edges) 정규화.

    좌표는 임의(0,0)로 둔다 — 프론트가 트리 자동 레이아웃(좌→우)으로 재배치하므로 무의미.
    재귀로 N단계 깊이까지 모두 펼친다.
    """
    import time
    uid_counter = [0]
    def uid(prefix: str) -> str:
        uid_counter[0] += 1
        return f"{prefix}_{int(time.time() * 1000)}_{uid_counter[0]}"

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    central_id = uid("n")
    nodes.append({
        "id": central_id, "x": 0, "y": 0, "w": 200, "h": 36,
        "part": "shared", "title": (central_title or "마인드맵")[:80],
        "meta": "", "body": "", "highlighted": True,
    })

    def add_branch(parent_id: str, br: Dict[str, Any], depth: int, root_part: str):
        if not isinstance(br, dict):
            return
        b_id = uid("n")
        origin = (br.get("origin") or [])
        meta_str = " · ".join(origin[:2]) if origin else ""
        nodes.append({
            "id": b_id, "x": 0, "y": 0,
            "w": 180 if depth == 1 else 160,
            "h": 36,
            "part": root_part,
            "title": (br.get("title") or "")[:80],
            "meta": meta_str[:80],
            "body": (br.get("summary") or "")[:240],
            "highlighted": False,
        })
        edges.append({
            "id": uid("e"), "from": parent_id, "to": b_id,
            "label": "", "style": "curve",
        })
        # 자손 재귀 (깊이 가드 4)
        if depth < 4:
            for ch in (br.get("children") or []):
                add_branch(b_id, ch, depth + 1, root_part)

    for i, br in enumerate(branches or []):
        root_part = _PARTS[i % len(_PARTS)]
        add_branch(central_id, br, 1, root_part)

    return {
        "mode": "mindmap",
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
