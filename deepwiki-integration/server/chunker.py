"""텍스트 청킹 — 토큰 단위로 분할 (오버랩 포함)."""
import re
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


# [r96] 함수/클래스/마크다운 헤더 시그니처 패턴 — 의미 단위 분할용
# 큰 단일 파일(public/index.html ~60K 토큰)에서 dbUpsertCategory 같은 식별자가
# 다른 무관한 코드와 섞여 하나의 청크에 들어가는 문제를 해결.
_SIGNATURE_PATTERNS = [
    # JavaScript / TypeScript
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+\w+",
    r"^\s*(?:export\s+)?const\s+\w+\s*=\s*(?:async\s*)?(?:function|\()",
    r"^\s*(?:export\s+)?(?:async\s+)?(?:var|let)\s+\w+\s*=\s*function",
    r"^\s*(?:export\s+)?class\s+\w+",
    # Python
    r"^\s*(?:async\s+)?def\s+\w+",
    r"^\s*class\s+\w+",
    # C / C++ / C# / Java / Go / Rust / Kotlin (간단)
    r"^\s*(?:public|private|protected|static|async|fn)\s+[\w<>\[\],\s]+\s+\w+\s*\(",
    # Markdown headings (## ~ ####)
    r"^#{1,4}\s+\S",
    # 대형 주석 헤더 (// ────, # =====, /* === ===)
    r"^\s*//\s*[=─\-—]{5,}",
    r"^\s*#\s*[=─\-—]{5,}",
    r"^\s*/\*+\s*[=─\-—]{3,}",
]
_SIGNATURE_RE = re.compile("|".join(_SIGNATURE_PATTERNS), re.MULTILINE)
# 청크 최소 크기 (너무 잘게 쪼개지면 컨텍스트 손실)
_MIN_CHUNK_TOKENS = 80


def _split_by_signatures(text: str, chunk_size: int) -> List[str]:
    """[r96] 함수/클래스/헤더 시그니처 라인 위치를 기준으로 텍스트 분할.

    Returns: 시그니처에서 시작하는 의미 단위 블록 리스트.
    각 블록은 다음 시그니처 직전까지 포함. 빈 줄 무시 가능.

    예시 (JS):
        async function dbUpsertCategory(cat) { ... }   ← 블록 1
        async function dbUpsertTask(task) { ... }      ← 블록 2

    예시 (MD):
        ## 1. 칸반 모델                                  ← 블록 1
        ## 2. 스프린트                                   ← 블록 2
    """
    positions = [m.start() for m in _SIGNATURE_RE.finditer(text)]
    if len(positions) < 2:
        # 시그니처 부족 → 빈줄 3+ 폴백
        return []
    positions = [0] + positions + [len(text)]
    blocks = []
    for i in range(len(positions) - 1):
        block = text[positions[i]:positions[i + 1]].strip()
        if block:
            blocks.append(block)
    # 너무 작은 블록은 다음 블록에 합치기 (의미 손실 방지)
    merged = []
    buffer = ""
    buffer_tokens = 0
    for block in blocks:
        bt = count_tokens(block)
        if buffer_tokens + bt > chunk_size and buffer:
            merged.append(buffer.strip())
            buffer = block
            buffer_tokens = bt
        elif buffer_tokens < _MIN_CHUNK_TOKENS:
            # 너무 작으면 버퍼에 누적
            buffer += ("\n\n" if buffer else "") + block
            buffer_tokens += bt
        else:
            # 적정 크기 → 새 청크 시작
            merged.append(buffer.strip())
            buffer = block
            buffer_tokens = bt
    if buffer.strip():
        merged.append(buffer.strip())
    return merged


def chunk_code(text: str, file_path: str = "") -> List[str]:
    """코드/장문 텍스트 청킹.

    [r96] 우선 함수·클래스·헤더 시그니처 기반 의미 분할 시도.
    실패하면 빈 줄 3개 기준, 그것도 실패하면 토큰 기반 균등 분할로 폴백.
    """
    text = (text or "").strip()
    if not text:
        return []

    chunk_size = settings.CHUNK_SIZE

    # [r96] 1순위: 함수/클래스/헤더 시그니처 기반 분할 — 의미 응집 최우선
    sig_chunks = _split_by_signatures(text, chunk_size)
    if sig_chunks and len(sig_chunks) >= 2:
        # 너무 큰 청크가 있으면 추가로 chunk_text 분할 적용
        result = []
        for c in sig_chunks:
            if count_tokens(c) > chunk_size:
                result.extend(chunk_text(c, chunk_size=chunk_size))
            else:
                result.append(c)
        return result

    # 2순위: 빈 줄 3+ 기준 블록 분리 (레거시 휴리스틱)
    blocks = text.split("\n\n\n")
    if len(blocks) > 1:
        chunks = []
        current = ""
        current_tokens = 0
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
        if chunks:
            return chunks

    # 3순위: 균등 토큰 분할 (최후)
    return chunk_text(text)
