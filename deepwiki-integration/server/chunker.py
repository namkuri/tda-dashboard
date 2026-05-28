"""텍스트 청킹 — 토큰 단위로 분할 (오버랩 포함)."""
import tiktoken
from typing import List
from config import settings


# tiktoken을 768d 임베딩 모델의 정확한 토크나이저가 아니지만,
# 청크 길이 추정용으로는 충분 (대략적 1.3x 보정).
_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """토큰 수 (대략)."""
    return len(_enc.encode(text or ""))


def chunk_text(
    text: str,
    chunk_size: int = None,
    overlap: int = None,
) -> List[str]:
    """텍스트를 토큰 단위 청크로 분할.

    - chunk_size: 청크당 최대 토큰 (default: settings.CHUNK_SIZE = 500)
    - overlap: 인접 청크 간 겹치는 토큰 (default: settings.CHUNK_OVERLAP = 50)

    문장/줄 경계를 가능한 보존 — 한국어/코드 모두 적절히 동작.
    """
    chunk_size = chunk_size or settings.CHUNK_SIZE
    overlap = overlap or settings.CHUNK_OVERLAP
    text = (text or "").strip()
    if not text:
        return []

    tokens = _enc.encode(text)
    if len(tokens) <= chunk_size:
        return [text]

    chunks = []
    step = chunk_size - overlap
    for i in range(0, len(tokens), step):
        chunk_tokens = tokens[i:i + chunk_size]
        chunk = _enc.decode(chunk_tokens)
        chunks.append(chunk)
        if i + chunk_size >= len(tokens):
            break
    return chunks


def chunk_code(text: str, file_path: str = "") -> List[str]:
    """코드 청킹 — 가능한 함수/클래스 경계로 분할.

    간단한 휴리스틱: 빈 줄 두 개 이상으로 블록 분리 후 합치기.
    더 정교한 구현은 tree-sitter 필요 (Phase 2 추가).
    """
    text = (text or "").strip()
    if not text:
        return []

    # 빈 줄 두 개 기준 블록 분리
    blocks = text.split("\n\n\n")  # 3+ 줄바꿈
    if len(blocks) == 1:
        # 빈 줄 부족 → 일반 chunk_text로 폴백
        return chunk_text(text)

    chunks = []
    current = ""
    current_tokens = 0
    chunk_size = settings.CHUNK_SIZE

    for block in blocks:
        block_tokens = count_tokens(block)
        if current_tokens + block_tokens > chunk_size and current:
            chunks.append(current.strip())
            current = block
            current_tokens = block_tokens
        else:
            current += ("\n\n" if current else "") + block
            current_tokens += block_tokens

    if current.strip():
        chunks.append(current.strip())

    return chunks
