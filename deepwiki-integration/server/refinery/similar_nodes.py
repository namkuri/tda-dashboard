"""[r223] 유사 노드 임베딩 그룹핑 — MECE 위반 감지.

cosine 유사도 0.85+ 노드를 그룹으로 묶어 사용자에게 "중복 의심" 표시.
"""
from typing import List, Dict, Any, Tuple
import math

from ollama_client import get_ollama


async def find_similar_groups(nodes: List[Dict[str, Any]], threshold: float = 0.85) -> List[List[str]]:
    """[[node_id, node_id, ...], ...] 형태로 그룹 반환."""
    ollama = get_ollama()
    leaf_nodes = [n for n in nodes if n.get("kind") != "category" and n.get("title")]
    if len(leaf_nodes) < 2:
        return []
    texts = [(n["id"], f"{n['title']} — {(n.get('summary') or '')[:200]}") for n in leaf_nodes]
    embeddings = []
    for nid, text in texts:
        try:
            emb = await ollama.embed(text)
            embeddings.append((nid, emb))
        except Exception:
            continue
    # 페어 cosine
    groups: List[List[str]] = []
    seen = set()
    for i, (id_i, emb_i) in enumerate(embeddings):
        if id_i in seen:
            continue
        group = [id_i]
        for j in range(i + 1, len(embeddings)):
            id_j, emb_j = embeddings[j]
            if id_j in seen:
                continue
            sim = _cosine(emb_i, emb_j)
            if sim >= threshold:
                group.append(id_j)
                seen.add(id_j)
        if len(group) >= 2:
            groups.append(group)
            seen.add(id_i)
    return groups


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
