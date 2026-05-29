"""Ollama API 래퍼 — 임베딩 + LLM 추론 (스트리밍 포함)."""
import httpx
import json
import re
from typing import AsyncIterator, List, Dict, Any
from config import settings


# [r109] Qwen이 tool_calls 필드 대신 content에 JSON으로 도구 호출을 적어올 때 추출.
# 패턴 1: ```json { "name": ..., "arguments": ... } ```
# 패턴 2: raw JSON { "name": ..., "arguments": ... }
# 패턴 3: 배열 [ {...}, {...} ]
# 패턴 4: <tool_call>{...}</tool_call> (Qwen native 형식)
_JSON_BLOCK_RE = re.compile(r"```(?:json|tool_call)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _parse_tool_calls_from_content(content: str) -> List[Dict[str, Any]]:
    """content에서 도구 호출 JSON 추출 → OpenAI tool_calls 형식 변환.

    반환: [{"function": {"name": "...", "arguments": {...}}, ...]
    """
    if not content or not content.strip():
        return []
    candidates: List[str] = []
    text = content.strip()

    # <tool_call> 태그 (Qwen native)
    for m in _TOOL_CALL_TAG_RE.finditer(text):
        candidates.append(m.group(1))

    # ```json 또는 ```tool_call 블록
    for m in _JSON_BLOCK_RE.finditer(text):
        candidates.append(m.group(1))

    # 텍스트 전체가 JSON으로 시작/끝나면 그대로 시도
    if text.startswith("{") and text.endswith("}"):
        candidates.append(text)
    if text.startswith("[") and text.endswith("]"):
        candidates.append(text)

    # 첫/마지막 { } 사이 substring (fallback — 텍스트 내 JSON 한 덩어리)
    if not candidates:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            candidates.append(text[s:e + 1])

    out: List[Dict[str, Any]] = []
    seen = set()
    for c in candidates:
        try:
            obj = json.loads(c)
        except Exception:
            continue
        items = obj if isinstance(obj, list) else [obj]
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("name") or it.get("function") or it.get("tool")
            if isinstance(name, dict):  # {"function": {"name": ...}} 형태
                name = name.get("name")
            if not name or not isinstance(name, str):
                continue
            args = it.get("arguments") or it.get("parameters") or it.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            sig = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if sig in seen:
                continue
            seen.add(sig)
            out.append({"function": {"name": name, "arguments": args}})
    return out


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

    async def warmup(self, model: str = None, timeout: float = 600.0) -> Dict[str, Any]:
        """[r135] 모델을 메모리에 로드 — 큰 LLM 호출 전 예열.

        매우 짧은 더미 요청(num_predict=1) 으로 모델 weights 를 VRAM/RAM 에 올림.
        반환: {ok: bool, elapsed_sec: float, message: str, response_chars: int}
        timeout 기본 10분 (32B 콜드 스타트는 5분+ 가능).
        """
        import time as _t
        model = model or settings.LLM_MODEL
        t0 = _t.time()
        try:
            r = await self.client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with just 'ok'"}],
                    "stream": False,
                    "options": {"num_predict": 4, "temperature": 0},
                    "keep_alive": "30m",
                },
                timeout=timeout,
            )
            elapsed = _t.time() - t0
            if r.status_code != 200:
                return {
                    "ok": False, "elapsed_sec": elapsed, "response_chars": 0,
                    "message": f"HTTP {r.status_code}: {r.text[:200]}",
                }
            data = r.json()
            content = (data.get("message") or {}).get("content", "") or ""
            return {
                "ok": True, "elapsed_sec": elapsed, "response_chars": len(content),
                "message": f"모델 '{model}' 로드 완료 ({elapsed:.1f}초, 응답 {len(content)}자)",
            }
        except httpx.TimeoutException:
            elapsed = _t.time() - t0
            return {
                "ok": False, "elapsed_sec": elapsed, "response_chars": 0,
                "message": f"모델 '{model}' 로드 타임아웃({elapsed:.1f}초) — VRAM/RAM 부족 또는 모델 다운로드 중일 가능성. 'ollama list' / 'ollama ps' 확인",
            }
        except Exception as e:
            elapsed = _t.time() - t0
            return {
                "ok": False, "elapsed_sec": elapsed, "response_chars": 0,
                "message": f"모델 '{model}' 로드 실패 ({type(e).__name__}): {e}",
            }

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
        num_ctx: int = 16384,  # [r133] 8192 → 16384 — 큰 카테고리 프롬프트 안 잘리게
    ) -> AsyncIterator[str]:
        """LLM 채팅 스트리밍 (텍스트 전용).

        messages: [{"role": "system|user|assistant", "content": "..."}]
        yields: 응답 텍스트 청크 (delta only).

        [r133] num_ctx 인자로 호출별 컨텍스트 크기 지정 가능 (기본 16K).
        """
        model = model or settings.LLM_MODEL
        async with self.client.stream(
            "POST",
            f"{self.base_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": True,
                "options": {"temperature": temperature, "num_ctx": num_ctx},
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
        """[r108→r109] Tool calling 1회 호출 (비스트리밍) + content fallback.

        Returns: { "role": "assistant", "content": "...", "tool_calls": [...] }

        Qwen 2.5 Coder + Ollama는 가끔 tool_calls 필드 대신 content에 JSON
        텍스트로 도구 호출을 적어옴. r109에서 그 케이스 fallback 파싱.
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
        content = msg.get("content", "") or ""
        tool_calls = msg.get("tool_calls", []) or []
        # [r109] tool_calls 비어있고 content에 JSON 도구 호출 있으면 fallback 파싱
        if not tool_calls and content:
            parsed = _parse_tool_calls_from_content(content)
            if parsed:
                tool_calls = parsed
                content = ""  # content는 도구 호출 표시였으므로 비움
        return {
            "role": msg.get("role", "assistant"),
            "content": content,
            "tool_calls": tool_calls,
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
