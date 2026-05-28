"""Retriever — 질문 임베딩 + pgvector top-k 검색."""
from typing import List, Optional, Dict, Any
from ollama_client import get_ollama
from supabase_store import get_store
from config import settings


async def retrieve(
    query: str,
    project_id: Optional[str] = None,
    source_types: Optional[List[str]] = None,
    top_k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """질문 → 임베딩 → 유사도 검색.

    Returns: 상위 K개 청크 [{source_type, source_id, source_title, content, similarity}, ...]
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
    # 유사도 0.3 미만은 노이즈로 제외
    return [r for r in results if r.get("similarity", 0) >= 0.3]
