"""Retriever — 질문 임베딩 + pgvector top-k 검색."""
from typing import List, Optional, Dict, Any
from ollama_client import get_ollama
from supabase_store import get_store
from config import settings


# [r93] 유사도 임계치 — nomic-embed-text는 cosine sim이 보통 0.2~0.7 범위라
# 0.3은 너무 엄격해서 짧은 질문에서 결과가 없음. 0.15로 완화.
SIMILARITY_THRESHOLD = 0.15


async def retrieve(
    query: str,
    project_id: Optional[str] = None,
    source_types: Optional[List[str]] = None,
    top_k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """질문 → 임베딩 → 유사도 검색.

    Returns: 상위 K개 청크 [{source_type, source_id, source_title, content, similarity}, ...]

    유사도 임계치 미만은 노이즈로 제외하되, 결과가 너무 적으면 임계치 무시하고
    top-K 그대로 반환 (사용자가 어쨌든 뭐라도 보게).
    """
    top_k = top_k or settings.TOP_K
    ollama = get_ollama()
    store = get_store()
    embedding = await ollama.embed(query)
    results = store.search(
        query_embedding=embedding,
        project_id=project_id,
        source_types=source_types,
        top_k=top_k,
    )
    if not results:
        return []
    # 1차: 임계치 필터
    filtered = [r for r in results if r.get("similarity", 0) >= SIMILARITY_THRESHOLD]
    # 2차: 너무 적으면 (3개 미만) 원본 top-K 반환 — 사용자가 빈 답변보다 약한 컨텍스트라도 받음
    if len(filtered) < 3:
        return results[:max(3, top_k // 2)]
    return filtered
