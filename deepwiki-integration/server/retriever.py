"""Retriever — 질문 임베딩 + pgvector top-k 검색."""
from typing import List, Optional, Dict, Any
from ollama_client import get_ollama
from supabase_store import get_store
from config import settings


# [r93→r94] 유사도 임계치 — nomic-embed-text는 cosine sim이 보통 0.2~0.7 범위.
# 짧은 한국어 질문 → 긴 영문 코드 청크 사이 유사도가 낮게 측정되는 경향.
# r93에 0.15로 완화, r94에 0.10으로 추가 완화 + "필터링 후 적으면 폴백" 로직 유지.
SIMILARITY_THRESHOLD = 0.10

# [r96] 같은 source_id에서 가져올 최대 청크 수 — 한 큰 파일이 결과 전체를 점유하는 문제 해결.
# 예: 60K 토큰 보고서 1개 파일이 top-10을 7개 채워서 다른 파일이 안 보이는 현상 방지.
MAX_CHUNKS_PER_SOURCE = 2


async def retrieve(
    query: str,
    project_id: Optional[str] = None,
    source_types: Optional[List[str]] = None,
    top_k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """질문 → 임베딩 → 유사도 검색 → 다양성 dedup.

    Returns: 상위 K개 청크 [{source_type, source_id, source_title, content, similarity}, ...]

    1) 임계치 이상 결과만 필터
    2) source_id별 최대 N개 청크만 유지 (다양성)
    3) 결과가 너무 적으면 임계치 무시 폴백
    """
    top_k = top_k or settings.TOP_K
    ollama = get_ollama()
    store = get_store()
    embedding = await ollama.embed(query)
    # [r96] dedup 위해 더 많이 가져온 뒤 줄임 — top_k × 3
    fetch_count = max(top_k * 3, 24)
    results = store.search(
        query_embedding=embedding,
        project_id=project_id,
        source_types=source_types,
        top_k=fetch_count,
    )
    if not results:
        return []

    # 1차: 임계치 필터
    filtered = [r for r in results if r.get("similarity", 0) >= SIMILARITY_THRESHOLD]
    # 폴백: 너무 적으면 원본 상위 전부
    if len(filtered) < 3:
        filtered = results

    # 2차: source_id별 최대 N개 청크만 유지 (다양성)
    diversified = _dedup_by_source(filtered, max_per_source=MAX_CHUNKS_PER_SOURCE)

    # 3차: 최종 top_k 자르기
    return diversified[:top_k]


def _dedup_by_source(chunks: List[Dict[str, Any]], max_per_source: int = 2) -> List[Dict[str, Any]]:
    """[r96] 같은 source_id의 청크를 max_per_source개까지만 유지.

    입력 순서(유사도 내림차순)를 보존 — 각 source의 최상위 청크들이 살아남음.
    """
    counts: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    for c in chunks:
        sid = c.get("source_id") or "(unknown)"
        n = counts.get(sid, 0)
        if n >= max_per_source:
            continue
        counts[sid] = n + 1
        out.append(c)
    return out
