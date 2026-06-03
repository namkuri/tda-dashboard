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
try:
    from pypdf import PdfReader as _PdfReader  # [r205]
    _HAS_PDF = True
except Exception:
    try:
        from PyPDF2 import PdfReader as _PdfReader  # 폴백
        _HAS_PDF = True
    except Exception:
        _HAS_PDF = False


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
            # [r205] PDF
            if "pdf" in ct or lower_url.endswith(".pdf"):
                if not _HAS_PDF:
                    return ""
                try:
                    reader = _PdfReader(BytesIO(r.content))
                    n = min(len(reader.pages), 200)  # 폭주 가드
                    parts = []
                    for i in range(n):
                        try:
                            txt = reader.pages[i].extract_text() or ""
                            if txt.strip():
                                parts.append(txt.strip())
                        except Exception:
                            continue
                    return "\n\n".join(parts)[:max_chars]
                except Exception:
                    return ""
            return ""
    except Exception:
        return ""


# [r215] 마인드맵 시스템 메시지 — 메타 지시는 모두 system 으로 격리.
# user 메시지에는 콘텐츠만. LLM 이 메타를 콘텐츠로 착각해 마인드맵 노드로
# 만드는 사고(예: 'MECE 원칙', '노드 텍스트', '키워드 1~5단어' 같은 프롬프트
# 어구가 그대로 노드로 출력되는 현상) 차단.
_MINDMAP_SYSTEM = """당신은 문서를 마인드맵 JSON 으로 변환하는 도구입니다.

[엄격 규칙 — 이 문장들은 마인드맵 콘텐츠가 아닙니다]
1. 출력은 ```json ... ``` 코드 펜스 안의 JSON 한 개만. 그 외 텍스트 금지.
2. 노드 텍스트(title)는 **반드시 사용자가 보낸 문서 본문에서 발견되는 개념**만
   사용. 본 시스템 메시지(역할/규칙/MECE/계층/노드 텍스트 같은 어구)는
   절대 마인드맵 노드가 되어서는 안 됩니다. 위반 시 즉시 다시 생성하세요.
3. 노드 텍스트는 1~5단어 이내의 키워드·단문(서술형 금지).
4. 중심 → 1레벨 4~7개 → 2레벨 3~7개 → 3레벨 2~6개. 의미 있으면 4~5레벨 확장.
   1레벨은 서로 의미가 겹치지 않게, 합치면 문서를 빠짐없이 포괄.
5. 한국어. 고유명사·기술용어(영문)는 그대로 유지.
6. 사용자 문서 본문이 너무 짧거나 비어있으면 central 만 두고
   branches: [] 로 응답하세요. 절대 시스템 메시지 어구를 노드로 만들지 마세요.

[출력 형식 — JSON 스키마]
```json
{
  "central": "중심 주제 (문서 제목 기반)",
  "branches": [
    { "title": "1레벨 키워드", "children": [
      { "title": "2레벨 키워드", "children": [
        { "title": "3레벨 키워드" }
      ]}
    ]}
  ]
}
```
"""


_SINGLE_PROMPT = """다음은 마인드맵으로 변환할 문서입니다. **이 문서 본문 안에 등장하는 개념만**
노드로 추출하세요. 본문 밖의 일반 지식이나 시스템 지시 어구는 노드가 될 수 없습니다.

<<<DOCUMENT_TITLE>>>
{title}
<<<END_DOCUMENT_TITLE>>>

<<<DOCUMENT_BODY>>>
{content}
<<<END_DOCUMENT_BODY>>>

위 <<<DOCUMENT_BODY>>> 안의 텍스트만 마인드맵 소스입니다. 시스템 메시지의 규칙
어구(예: 'MECE', '1레벨', '노드 텍스트', '키워드 1~5단어')는 절대 노드 title 이
되어서는 안 됩니다.

JSON 한 개만 출력:
"""


_MULTI_PROMPT = """다음은 마인드맵으로 통합 변환할 {doc_count}개 문서입니다. **이 문서들 본문 안에
등장하는 개념만** 노드로 추출하세요. 본문 밖의 일반 지식이나 시스템 지시 어구는
노드가 될 수 없습니다. cross_links 도 본문 안 개념끼리만.

<<<DOCUMENTS_BODY>>>
{doc_summaries}
<<<END_DOCUMENTS_BODY>>>

위 <<<DOCUMENTS_BODY>>> 안의 텍스트만 마인드맵 소스입니다. 시스템 메시지의 규칙
어구(예: 'MECE', '1레벨', '노드 텍스트', '키워드 1~5단어')는 절대 노드 title 이
되어서는 안 됩니다.

JSON 한 개만 출력. branches[] 각 노드에 origin([문서 제목 일부]) 를 채워주세요.
cross_links[] 는 본문 안 개념끼리만 (다른 문서에 등장한 같은 개념의 인과·대비·보완).
"""


_PARTS = ['ai', 'planning', 'shared', 'scenario', 'terrain', 'extra1', 'extra2']


# [r215] 메타 키워드 — 시스템 프롬프트에서 유래한 어구가 노드 title 로 출력되는
# 누출 현상 감지·필터. 일치하거나 부분 포함되면 노드 제거.
_META_LEAK_KEYWORDS = [
    "mece", "mutually exclusive", "collectively exhaustive",
    "프레임워크 개요",  # "MECE 원칙·키워드 압축" 컨텍스트의 표제 어구
    "1레벨", "2레벨", "3레벨", "4레벨", "5레벨",
    "주가지", "서브가지", "세부 노드",
    "노드 텍스트", "단문 길이", "단문 길이 제한",
    "키워드 1~5단어", "1~5단어", "1-5 단어",
    "중심 주제", "중심 노드",
    "사고 절차", "출력 형식", "작성 규칙",
    "json 한 개", "코드 펜스",
    "독립성", "포괄성", "전체 포괄", "모든 내용 포함", "중복 없음", "서로 겹치지 않음",
    "구성 요소",  # "구성 요소" 같은 메타 분류
    "procedural generation",  # 시스템 프롬프트의 예시 어구 — 사용자 문서엔 안 나옴 가정
]


def _is_meta_leak(text: str) -> bool:
    """노드 title 이 시스템 프롬프트 누출인지 판정."""
    if not text:
        return False
    t = text.strip().lower()
    for kw in _META_LEAK_KEYWORDS:
        if kw in t:
            return True
    return False


def _scrub_meta_branches(branches: List[Dict[str, Any]]) -> tuple:
    """branches 트리에서 메타 누출 노드 제거 (재귀).

    제거 정책:
      1) title 이 명시적 메타 키워드 매칭 → 즉시 제거
      2) 자식이 있었는데 1)/2) 로 모두 제거돼 비게 되면 부모도 제거
         (예: '키워드' 노드 자체는 통과, 자식이 '1~5단어'/'단문 길이'만 있어
          전부 제거되면 부모 '키워드' 도 누출 추정해 함께 제거)
    """
    if not isinstance(branches, list):
        return [], 0
    cleaned = []
    removed = 0
    for br in branches:
        if not isinstance(br, dict):
            continue
        title = br.get("title") or ""
        if _is_meta_leak(title):
            removed += 1
            continue
        kids = br.get("children")
        had_kids = isinstance(kids, list) and len(kids) > 0
        if had_kids:
            br["children"], r2 = _scrub_meta_branches(kids)
            removed += r2
            # 모든 자식이 메타로 제거되어 비었으면 부모도 누출 추정
            if not br["children"]:
                removed += 1
                continue
        cleaned.append(br)
    return cleaned, removed


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

    # [r215] 콘텐츠 길이 가드 — 너무 짧으면 LLM 이 시스템 프롬프트 규칙을 콘텐츠로
    # 착각하고 메타 어구('MECE', '노드 텍스트', '1~5단어')를 그대로 마인드맵 노드로
    # 출력하는 사고 방지. 정제 후 글자수 기준 150자 미만이면 거부.
    def _content_chars(s: str) -> int:
        return len(re.sub(r"\s+", "", s or ""))

    if is_multi:
        # 다중 — 문서별 요약 블록
        blocks = []
        total_chars = 0
        for d in docs[:8]:
            title = (d.get("title") or "").strip() or "(제목 없음)"
            content = (d.get("content") or "")[:3000]
            total_chars += _content_chars(content)
            blocks.append(f"### 📄 {title}\n\n{content}")
        if total_chars < 200:
            yield {"event": "error", "message": (
                f"문서 본문이 너무 짧습니다(총 {total_chars}자). "
                "마인드맵 생성을 위해 각 문서에 최소 200자 이상의 본문이 필요합니다. "
                "본문을 더 작성하거나 다른 문서를 선택해 다시 시도하세요."
            )}
            return
        doc_summaries = "\n\n---\n\n".join(blocks)
        prompt = _safe_format(_MULTI_PROMPT, doc_count=len(docs), doc_summaries=doc_summaries)
        yield {"event": "stage", "stage": "llm", "message": "LLM에 통합 마인드맵 요청 중..."}
    else:
        d = docs[0]
        title = (d.get("title") or "").strip() or "(제목 없음)"
        content = (d.get("content") or "")[:9000]
        if _content_chars(content) < 150:
            yield {"event": "error", "message": (
                f"문서 본문이 너무 짧습니다({_content_chars(content)}자). "
                "마인드맵 생성을 위해 최소 150자 이상의 본문이 필요합니다. "
                "문서에 내용을 더 작성한 뒤 다시 시도하세요."
            )}
            return
        prompt = _safe_format(_SINGLE_PROMPT, title=title, content=content)
        yield {"event": "stage", "stage": "llm", "message": f"LLM에 단일 마인드맵 요청 중... ({title})"}

    # LLM 호출 — [r215] 시스템 메시지에 메타 규칙 격리, user 에는 콘텐츠만.
    chunks: List[str] = []
    try:
        async for delta in ollama.chat_stream(
            messages=[
                {"role": "system", "content": _MINDMAP_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            model=model,
            temperature=0.4,
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

    # [r215] 시스템 프롬프트 누출 노드 필터링
    if _is_meta_leak(central):
        # central 자체가 누출이면 문서 제목으로 폴백
        if not is_multi:
            central = (docs[0].get("title") or "마인드맵").strip()[:60]
        else:
            central = "통합 마인드맵"
    branches, removed = _scrub_meta_branches(branches)
    if removed:
        yield {"event": "warn", "message": (
            f"⚠ 시스템 프롬프트 누출 노드 {removed}개 자동 제거됨 "
            "(LLM 이 메타 규칙을 콘텐츠로 출력한 경우)."
        )}
    if not branches:
        yield {"event": "error", "message": (
            "마인드맵 생성 결과가 모두 시스템 프롬프트 누출이라 폐기됐습니다. "
            "문서 본문이 충분한지 확인 후 다시 시도해 주세요."
        )}
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
