"""Retriever — 질문 임베딩 + pgvector top-k 검색."""
from typing import List, Optional, Dict, Any
from ollama_client import get_ollama
from supabase_store import get_store
from config import settings


# [r93→r94] 유사도 임계치 — nomic-embed-text는 cosine sim이 보통 0.2~0.7 범위.
# 짧은 한국어 질문 → 긴 영문 코드 청크 사이 유사도가 낮게 측정되는 경향.
# r93에 0.15로 완화, r94에 0.10으로 추가 완화 + "필터링 후 적으면 폴백" 로직 유지.
SIMILARITY_THRESHOLD = 0.10


async def retrieve(
    query: str,
    project_id: Optional[str] = None,
    source_types: Optional[List[str]] = None,
    top_k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """질문 → 임베딩 → 유사도 검색.

    Returns: 상위 K개 청크 [{source_type, source_id, source_title, content, similarity}, ...]

    유사도 임계치 미만은 노이즈로 제외하되, 결과가 너무 적으면 임계치 무시하고
    top-K 그대로 반환 (LLM 시스템 프롬프트가 단서 활용을 책임지므로 약한 매칭도 통과).
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
    # 2차: 너무 적으면 원본 상위 K개 모두 반환 — 사용자가 빈 답변보다 약한 컨텍스트라도 받음.
    # LLM이 시스템 프롬프트(r94)에 따라 단서를 활용해 답변.
    if len(filtered) < 3:
        return results[:top_k]
    return filtered
