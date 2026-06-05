"""[r261] 자체 RAG(경량) — 기존 프로젝트 문서를 검색해 도출 LLM 컨텍스트를 보강.

프롬프트 v2 #10 "기존 프로젝트 상황 이해 + 자체 RAG". 임베딩 인프라 의존 없이,
분해 키워드와 기존 wiki_docs(제목·본문)의 **키워드 겹침**으로 관련 문서를 찾아
상위 스니펫을 컨텍스트에 덧붙인다. → LLM 이 기존 정의·결정을 보고 중복 회피·적합 배치.
(임베딩 기반 의미 검색은 후속 확장 여지.)
"""
import re
from typing import List, Dict, Any, Optional

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_\-]+|[가-힣]{2,}")


def node_keywords(nodes: List[Dict[str, Any]]) -> set:
    """분해 노드 제목 → 키워드 집합(소문자)."""
    kws = set()
    for n in (nodes or []):
        for t in _TOKEN.findall(n.get("title") or ""):
            if len(t) >= 2:
                kws.add(t.lower())
    return kws


def score_doc(title: str, content: str, kws: set) -> int:
    """문서(제목+본문 앞부분)와 키워드 겹침 점수. 제목 일치는 가중."""
    if not kws:
        return 0
    title_l = (title or "").lower()
    body_l = (content or "")[:2000].lower()
    sc = 0
    for k in kws:
        if k in title_l:
            sc += 3
        elif k in body_l:
            sc += 1
    return sc


def rank_docs(rows: List[Dict[str, Any]], kws: set, top: int = 5, max_chars: int = 1800) -> str:
    """문서 행 리스트 → 상위 관련 문서 스니펫 블록(LLM 컨텍스트용). 순수 함수(테스트 가능)."""
    scored = []
    for r in (rows or []):
        sc = score_doc(r.get("title"), r.get("content"), kws)
        if sc > 0:
            scored.append((sc, r))
    scored.sort(key=lambda x: -x[0])
    parts: List[str] = []
    used = 0
    for sc, r in scored[:top]:
        snip = (r.get("content") or "").strip().replace("\n", " ")[:300]
        line = f"- [{r.get('title') or '문서'}] {snip}"
        if used + len(line) > max_chars:
            break
        parts.append(line)
        used += len(line)
    if not parts:
        return ""
    return "[관련 기존 문서(RAG)]\n" + "\n".join(parts)


def _query_from_nodes(nodes: List[Dict[str, Any]], limit: int = 20) -> str:
    """분해 노드 → 의미검색 쿼리 문자열(대분류 + 주요 잎 제목)."""
    cats = [n.get("title") for n in (nodes or []) if n.get("kind") == "category" and n.get("title")]
    leaves = [n.get("title") for n in (nodes or []) if n.get("kind") != "category" and n.get("title")]
    parts = (cats[:6] + leaves[:14])[:limit]
    return " ".join(parts)[:500]


def _format_chunks(chunks: List[Dict[str, Any]], max_chars: int = 2000) -> str:
    parts: List[str] = []
    used = 0
    for c in (chunks or []):
        title = c.get("source_title") or c.get("source_path") or "자료"
        snip = (c.get("content") or "").strip().replace("\n", " ")[:300]
        if not snip:
            continue
        line = f"- [{c.get('source_type', '?')}:{title}] {snip}"
        if used + len(line) > max_chars:
            break
        parts.append(line)
        used += len(line)
    if not parts:
        return ""
    return "[관련 기존 자료(의미검색 RAG)]\n" + "\n".join(parts)


async def retrieve_semantic(
    *,
    nodes: List[Dict[str, Any]],
    project_id: Optional[str] = None,
    top_k: int = 8,
    max_chars: int = 2000,
) -> str:
    """[r262] 임베딩 의미검색 RAG — 기존 retriever(ollama.embed + pgvector + 하이브리드)
    를 재사용해 분해 키워드와 의미적으로 가까운 기존 자료(위키/코드/WBS/태스크)를 검색.
    실패(임베딩/DB 미가용) 시 빈 문자열."""
    q = _query_from_nodes(nodes)
    if not q:
        return ""
    try:
        from retriever import retrieve
        chunks = await retrieve(q, project_id=project_id,
                                source_types=["wiki", "code", "wbs", "task", "issue"], top_k=top_k)
    except Exception:
        return ""
    return _format_chunks(chunks, max_chars)


def retrieve_relevant(
    *,
    nodes: List[Dict[str, Any]],
    project_id: Optional[str] = None,
    store: Any = None,
    top: int = 5,
    max_chars: int = 1800,
) -> str:
    """기존 wiki_docs 를 키워드로 검색 → 관련 스니펫 블록. 실패/무관 시 빈 문자열."""
    kws = node_keywords(nodes)
    if not kws:
        return ""
    try:
        if store is None:
            from supabase_store import get_store
            store = get_store()
        q = store.client.table("wiki_docs").select("title,content")
        if project_id:
            q = q.eq("project_id", project_id)
        rows = q.limit(150).execute().data or []
    except Exception:
        return ""
    return rank_docs(rows, kws, top=top, max_chars=max_chars)
