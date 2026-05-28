"""Ollama API 래퍼 — 임베딩 + LLM 추론 (스트리밍 포함)."""
import httpx
from typing import AsyncIterator, List
from config import settings


class OllamaClient:
    """비동기 Ollama 클라이언트.

    Ollama는 OpenAI 호환 API도 제공하지만, /api/embeddings와 /api/chat 네이티브 엔드포인트를 직접 사용.
    """

    def __init__(self, base_url: str = None):
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        # 임베딩은 짧지만, LLM 응답은 길 수 있어 충분한 timeout
        self.client = httpx.AsyncClient(timeout=300.0)

    async def close(self):
        await self.client.aclose()

    async def ping(self) -> bool:
        """Ollama 서버 살아있는지 확인."""
        try:
            r = await self.client.get(f"{self.base_url}/api/tags", timeout=3.0)
            return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> List[str]:
        """설치된 모델 목록."""
        try:
            r = await self.client.get(f"{self.base_url}/api/tags", timeout=5.0)
            data = r.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    async def embed(self, text: str, model: str = None) -> List[float]:
        """텍스트 임베딩. nomic-embed-text는 768d 반환."""
        model = model or settings.EMBED_MODEL
        r = await self.client.post(
            f"{self.base_url}/api/embeddings",
            json={"model": model, "prompt": text},
        )
        r.raise_for_status()
        return r.json()["embedding"]

    async def embed_batch(self, texts: List[str], model: str = None) -> List[List[float]]:
        """배치 임베딩 (Ollama는 단건만 지원 — 직렬 호출).

        대량 인덱싱 시 호출 횟수 많아져 느릴 수 있음. 향후 ollama 배치 지원되면 교체.
        """
        results = []
        for t in texts:
            v = await self.embed(t, model=model)
            results.append(v)
        return results

    async def chat_stream(
        self,
        messages: List[dict],
        model: str = None,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        """LLM 채팅 스트리밍.

        messages: [{"role": "system|user|assistant", "content": "..."}]
        yields: 응답 텍스트 청크 (delta only).
        """
        model = model or settings.LLM_MODEL
        async with self.client.stream(
            "POST",
            f"{self.base_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": True,
                "options": {"temperature": temperature, "num_ctx": 8192},
                "keep_alive": "30m",  # 모델 메모리 상주
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                import json
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("done"):
                    break
                msg = data.get("message", {})
                content = msg.get("content", "")
                if content:
                    yield content


# 싱글톤 (FastAPI 의존성으로 주입)
_ollama_singleton: OllamaClient = None


def get_ollama() -> OllamaClient:
    global _ollama_singleton
    if _ollama_singleton is None:
        _ollama_singleton = OllamaClient()
    return _ollama_singleton
