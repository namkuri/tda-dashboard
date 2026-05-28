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
        """LLM 채팅 스트리밍 (텍스트 전용).

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
                "keep_alive": "30m",
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

    async def chat_with_tools(
        self,
        messages: List[dict],
        tools: List[dict],
        model: str = None,
        temperature: float = 0.2,
    ) -> dict:
        """[r108] Tool calling 지원 1회 호출 (비스트리밍).

        Returns: { "role": "assistant", "content": "...", "tool_calls": [...] }
            tool_calls 가 있으면 호출자는 실행 후 새 message로 다시 chat_with_tools 호출.
            tool_calls 가 비어있으면 content 가 최종 답변.

        Ollama API spec: https://github.com/ollama/ollama/blob/main/docs/api.md
        Qwen 2.5 Coder는 OpenAI 호환 tool_calls 지원.
        """
        import json
        model = model or settings.LLM_MODEL
        r = await self.client.post(
            f"{self.base_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "options": {"temperature": temperature, "num_ctx": 8192},
                "keep_alive": "30m",
            },
            timeout=180.0,
        )
        r.raise_for_status()
        data = r.json()
        msg = data.get("message", {}) or {}
        return {
            "role": msg.get("role", "assistant"),
            "content": msg.get("content", "") or "",
            "tool_calls": msg.get("tool_calls", []) or [],
        }

    async def chat_stream_final(
        self,
        messages: List[dict],
        model: str = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        """[r108] 도구 호출 루프 마지막 단계 — 최종 답변 스트리밍.

        도구 호출 없는 일반 chat과 동일하지만 의미적으로 구분.
        """
        async for d in self.chat_stream(messages, model=model, temperature=temperature):
            yield d


# 싱글톤 (FastAPI 의존성으로 주입)
_ollama_singleton: OllamaClient = None


def get_ollama() -> OllamaClient:
    global _ollama_singleton
    if _ollama_singleton is None:
        _ollama_singleton = OllamaClient()
    return _ollama_singleton
