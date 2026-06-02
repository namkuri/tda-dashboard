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
from io import BytesIO
from typing import AsyncIterator, Dict, Any, List, Optional

from ollama_client import get_ollama
from wiki_generator import _safe_format  # [r147] brace KeyError 방지

# [r202] 첨부 파일 텍스트 추출 — HTML/DOCX/TXT/MD 지원. 실패해도 본문 생성은 진행.
try:
    import httpx  # type: ignore
    _HAS_HTTPX = True
except Exception:
    _HAS_HTTPX = False
try:
    from docx import Document as _DocxDoc  # python-docx
    _HAS_DOCX = True
except Exception:
    _HAS_DOCX = False


def _strip_html(text: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


async def _fetch_attachment(url: str, max_chars: int = 24000) -> str:
    """첨부 파일을 다운받아 텍스트로 변환. 실패 시 빈 문자열."""
    if not _HAS_HTTPX or not url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as cli:
            r = await cli.get(url)
            if r.status_code != 200:
                return ""
            ct = (r.headers.get("content-type", "") or "").lower()
            lower_url = url.lower().split("?")[0]
            if "html" in ct or lower_url.endswith((".html", ".htm")):
                try:
                    txt = r.text
                except Exception:
                    txt = r.content.decode("utf-8", errors="ignore")
                return _strip_html(txt)[:max_chars]
            if "wordprocessingml" in ct or lower_url.endswith(".docx"):
                if not _HAS_DOCX:
                    return ""
                try:
                    d = _DocxDoc(BytesIO(r.content))
                    parts = []
                    for p in d.paragraphs:
                        t = (p.text or "").strip()
                        if t:
                            parts.append(t)
                    # 표 내용도 추출
                    for tb in d.tables:
                        for row in tb.rows:
                            row_txt = " | ".join((cell.text or "").strip() for cell in row.cells)
                            if row_txt.strip():
                                parts.append(row_txt)
                    return "\n".join(parts)[:max_chars]
                except Exception:
                    return ""
            if "text/" in ct or lower_url.endswith((".txt", ".md", ".markdown")):
                try:
                    return r.text[:max_chars]
                except Exception:
                    return r.content.decode("utf-8", errors="ignore")[:max_chars]
            return ""
    except Exception:
        return ""


_SINGLE_PROMPT = """[역할]
당신은 복잡한 문서를 분석하여 구조화된 마인드맵 노드를 설계하는 **10년 차 정보 시각화 전문가**입니다.
당신의 강점은 (1) 문서의 숨은 계층 구조를 한 번에 파악하고, (2) MECE 원칙으로 군더더기 없이 분류하며,
(3) 긴 서술을 짧은 핵심 키워드로 압축하는 것입니다.

[임무]
아래 문서를 분석해 **[중심 주제 → 주가지(1레벨) → 서브가지(2레벨) → 세부 노드(3레벨)]** 형태의
계층 구조 마인드맵을 만드세요. 마지막에 JSON으로 출력합니다.

[입력 문서]
제목: {title}

```
{content}
```

[사고 절차 — 마음속으로 따라가세요. 출력하지는 마세요.]
1단계 (구조 잡기): 문서 전체를 훑어 **가장 중요한 대주제 4~7개**를 뽑습니다. 이 대주제들은
  **MECE 원칙**(Mutually Exclusive, Collectively Exhaustive)을 따라야 합니다 —
  서로 의미가 겹치지 않으면서, 합치면 문서 전체를 빠짐없이 포괄해야 합니다.
2단계 (살 붙이기): 각 대주제 아래 **서브가지(2레벨) 3~7개**를 키워드로 추출합니다.
  서브가지도 같은 대주제 안에서 MECE를 지향합니다.
3단계 (근거 추가): 의미상 더 풀 가치가 있는 곳에 **세부 노드(3레벨) 2~6개**를
  핵심 데이터·예시·근거·인용·정의·반례·표 항목 등으로 추가합니다.
  (모든 서브가지에 3레벨을 넣을 필요 없음. 짧게 다룬 부분은 2레벨에서 끝.)
4단계+ (깊이 확장 — 선택): 3레벨 중 **맥락상 더 깊이 풀 가치가 있는 노드**는
  주저 말고 **4레벨, 5레벨까지** 확장하세요. 예: '핵심 알고리즘 A' → 단계1·단계2·단계3 →
  각 단계의 입력/출력/제약조건. '용어 정의' → 하위 용어 → 그 용어의 예시.
  단, 깊이를 위한 깊이는 금지 — 의미가 빈약하면 멈추세요.

[작성 규칙]
1. **계층**: 기본 3단계, **의미상 더 깊이 풀 가치가 있는 가지는 4~5단계까지 자유롭게 확장**.
   균일하게 만들지 마세요 — 어떤 가지는 2단계에서 끝, 어떤 가지는 5단계까지 깊이.
   문서의 진짜 구조를 따라가세요. 마지막 계층을 기계적으로 2개로 끊지 마세요.
2. **노드 텍스트**: 문장이 아니라 **핵심 키워드 또는 1~5단어 이내의 단문**.
   - 좋은 예: "Procedural Generation", "타격 시스템", "P0 우선순위", "한·중·일 비교"
   - 나쁜 예(절대 금지): "이 시스템은 절차적 생성을 활용하여...", "여기서는 다음과 같은 점이 중요한데..."
   - summary는 1문장 이내(필요할 때만, 없어도 됨)
3. **MECE 원칙**: 1레벨은 반드시 MECE — 중복 없이, 전체 문서를 빠짐없이 포괄.
4. **풍부성**: 문서의 표·리스트·소제목·예시·반대 의견·각주·정의·인용·근거를 적극 노드로 변환.
5. **언어**: 한국어로 작성. 원문 고유명사·기술용어는 그대로 유지(예: "Procedural", "PMF", "ARPU").
6. **금지**: 출력에 설명문·도입어("이 마인드맵은...")·결론어·코드 펜스 밖 텍스트 금지.

[출력 형식 — 오직 JSON만, 다른 텍스트 금지]
children 안에 children을 계속 중첩해서 **4단계, 5단계까지 자유롭게 확장**할 수 있습니다.
의미상 더 풀 가치가 있는 가지만 깊이 들어가고, 단순한 가지는 얕게 두세요.

```json
{
  "central": "중심 주제 (1~6단어)",
  "branches": [
    {
      "title": "1레벨 키워드",
      "summary": "선택, 1문장 이내",
      "children": [
        {
          "title": "2레벨 키워드",
          "children": [
            {
              "title": "3레벨 키워드",
              "children": [
                {
                  "title": "4레벨 키워드",
                  "children": [
                    { "title": "5레벨 키워드" }
                  ]
                },
                { "title": "4레벨 키워드" }
              ]
            },
            { "title": "3레벨 키워드 (얕게 끝)" }
          ]
        },
        { "title": "2레벨 키워드 (얕게 끝)" }
      ]
    }
  ]
}
```
"""


_MULTI_PROMPT = """[역할]
당신은 여러 문서를 동시에 분석해 통합 마인드맵을 설계하는 **10년 차 정보 시각화 전문가**입니다.
당신의 강점은 (1) 서로 다른 문서들 사이의 공통 구조를 발견하고, (2) MECE 원칙으로 통합 분류하며,
(3) 문서 간 관계(인과·대비·보완)를 명시적으로 드러내는 것입니다.

[임무]
아래 {doc_count}개 문서를 모두 분석해 **공통 주제·핵심 개념·문서 간 관계**를 통합한
계층 구조 마인드맵을 만드세요. 출력은 JSON.

[입력 문서들]
{doc_summaries}

[사고 절차 — 마음속으로만, 출력 금지]
1단계 (공통 구조 찾기): 모든 문서를 훑어 **공통 대주제 5~8개**를 추출. 반드시 MECE.
  각 대주제가 어느 문서에서 비중 있게 다뤄졌는지 origin 배열로 기록.
2단계 (살 붙이기): 대주제별 서브가지 3~7개. 한 문서에만 있는 고유 개념도 누락하지 말고
  해당 대주제 아래 children으로 포함.
3단계 (세부): 의미 있는 곳에 3레벨 2~6개로 데이터·예시·근거 추가.
4단계+ (깊이 확장): 맥락상 더 깊이 풀 가치가 있는 노드는 **4~5단계까지** 자유롭게 확장.
  예: 두 문서가 공통으로 다룬 개념의 차이점을 4레벨로, 그 차이의 사례를 5레벨로.
5단계 (관계 명시): 서로 다른 문서/가지 사이의 의미 있는 연결을 cross_links에 기록.

[작성 규칙]
1. **계층**: 기본 3단계, **의미상 더 깊이 풀 가치 있는 가지는 4~5단계까지** 자유롭게 확장.
   균일성 강요 금지 — 풍부한 가지 많이, 단순한 가지 적게. 문서의 진짜 구조를 따라가세요.
2. **노드 텍스트**: 핵심 키워드 또는 1~5단어 단문. 서술형 금지.
3. **MECE**: 1레벨은 반드시 MECE.
4. **origin**: 각 노드에 어느 문서들에서 나왔는지 origin 배열로 표기.
5. **cross_links**: 문서 간 의미 있는 연결(인과·대비·보완·재사용) 명시.
6. **언어**: 한국어. 고유명사·기술용어는 원문 유지.

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
        # 자손 재귀 (깊이 가드 5) — [r202]
        if depth < 5:
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

    # [r202] 첨부 파일(HTML/DOCX/TXT/MD) 다운로드 후 본문에 합치기 — 문서의 첨부도 함께 분석
    att_total = 0
    for d in docs:
        urls = d.get("attachment_urls") or []
        if not urls:
            continue
        chunks_att = []
        for url in urls[:6]:  # 문서당 최대 6개
            yield {"event": "progress", "current": 0, "total": 0, "message": f"📎 첨부 읽는 중: {url[:90]}"}
            txt = await _fetch_attachment(url, max_chars=18000)
            if txt:
                chunks_att.append(f"\n\n--- 첨부 파일 ({url}) ---\n{txt}")
                att_total += 1
        if chunks_att:
            d["content"] = (d.get("content") or "") + "".join(chunks_att)
    if att_total:
        yield {"event": "info", "message": f"📎 첨부 파일 {att_total}개 본문 합쳐짐(HTML/DOCX/TXT/MD)"}

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
                {"role": "system", "content": "당신은 정보 시각화 전문가입니다. 사고는 단계적으로 하되 출력은 오직 유효한 JSON 하나만 합니다. MECE 원칙을 엄격히 지키고, 노드 텍스트는 항상 키워드·단문(1~5단어)으로 압축합니다. 서술형 문장은 금지입니다."},
                {"role": "user", "content": prompt},
            ],
            model=model,
            temperature=0.4,  # [r203] MECE 원칙·키워드 압축 안정성 위해 0.55→0.4. 단계 사고는 프롬프트로 유도.
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
