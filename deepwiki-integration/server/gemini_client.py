"""[r226] Gemini API 클라이언트 — Google Generative Language REST API.

ollama_client.OllamaClient 와 동일 인터페이스(chat_stream/ping/list_models)로
LLM 라우터(main.get_llm)가 모델명 prefix 로 투명하게 교체할 수 있게 한다.

무료 등급(2026.4+): Flash 계열만 — gemini-2.5-flash, gemini-2.5-flash-lite,
gemini-2.0-flash. Pro 모델은 유료. 정련소 분해/분류엔 Flash 로 충분.

웹 구독(Google AI Pro)과 무관 — API 키는 별도(무료 등급). 사용자가 발급한 키 사용.

주의:
- embed 는 미구현 (인덱싱 임베딩은 Ollama nomic-embed-text 유지).
- chat_with_tools(대화형 AI tool use) 는 미구현 — Ollama 권장. Gemini function
  calling 은 형식이 달라 별도 작업 필요.
"""
import json
from typing import AsyncIterator, List, Optional

try:
    import httpx
    _HAS_HTTPX = True
except Exception:
    _HAS_HTTPX = False


_BASE = "https://generativelanguage.googleapis.com/v1beta"

# 무료 등급에서 쓸 수 있는 Flash 계열 (2026.4 기준)
FREE_TIER_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]


class GeminiClient:
    def __init__(self, api_key: str, timeout: float = 180.0):
        if not api_key:
            raise ValueError("Gemini API 키가 필요합니다")
        self.api_key = api_key
        self.timeout = timeout
        # ollama_client 와 동일하게 self.client 보유 (httpx)
        self.client = httpx.AsyncClient(timeout=timeout) if _HAS_HTTPX else None

    async def ping(self) -> bool:
        """API 키 유효성 확인 — 모델 목록 조회 성공 여부."""
        if not _HAS_HTTPX:
            return False
        try:
            r = await self.client.get(f"{_BASE}/models?key={self.api_key}")
            return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> List[str]:
        """사용 가능 모델 목록 (generateContent 지원만)."""
        if not _HAS_HTTPX:
            return []
        try:
            r = await self.client.get(f"{_BASE}/models?key={self.api_key}")
            if r.status_code != 200:
                return []
            data = r.json()
            out = []
            for m in (data.get("models") or []):
                methods = m.get("supportedGenerationMethods") or []
                if "generateContent" in methods:
                    name = (m.get("name") or "").replace("models/", "")
                    if name:
                        out.append(name)
            return out
        except Exception:
            return []

    @staticmethod
    def _to_contents(messages: List[dict]):
        """[{role, content}] → (system_instruction, contents[])

        Gemini 형식:
          - system 메시지 → systemInstruction
          - user/assistant → contents[{role: 'user'|'model', parts:[{text}]}]
        """
        system_parts = []
        contents = []
        for m in messages:
            role = m.get("role")
            text = m.get("content") or ""
            if not text:
                continue
            if role == "system":
                system_parts.append(text)
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": text}]})
            else:  # user (기본)
                contents.append({"role": "user", "parts": [{"text": text}]})
        system_instruction = None
        if system_parts:
            system_instruction = {"parts": [{"text": "\n\n".join(system_parts)}]}
        return system_instruction, contents

    async def chat_stream(
        self,
        messages: List[dict],
        model: str = None,
        temperature: float = 0.3,
        num_ctx: int = 16384,  # 호환 — Gemini 는 자동 컨텍스트, 무시
        response_format: str = None,   # [r279] 'json' 이면 application/json 강제(코드펜스 없이 JSON 그대로)
        max_output_tokens: int = None, # [r279] 응답 토큰 상한(긴 회의록 truncation 방지)
    ) -> AsyncIterator[str]:
        """SSE 스트리밍 — ollama.chat_stream 과 동일 시그니처."""
        if not _HAS_HTTPX:
            raise RuntimeError("httpx 미설치 — Gemini 사용 불가")
        model = model or "gemini-2.5-flash"
        model = model.replace("models/", "")
        system_instruction, contents = self._to_contents(messages)
        gen_cfg = {"temperature": temperature}
        if response_format == "json":
            gen_cfg["responseMimeType"] = "application/json"
        if max_output_tokens:
            gen_cfg["maxOutputTokens"] = int(max_output_tokens)
        body = {
            "contents": contents,
            "generationConfig": gen_cfg,
        }
        if system_instruction:
            body["systemInstruction"] = system_instruction
        url = f"{_BASE}/models/{model}:streamGenerateContent?alt=sse&key={self.api_key}"
        async with self.client.stream("POST", url, json=body) as response:
            if response.status_code != 200:
                # 에러 본문 읽어 메시지화
                err_txt = ""
                try:
                    await response.aread()
                    err_txt = response.text[:300]
                except Exception:
                    pass
                raise RuntimeError(f"Gemini API {response.status_code}: {err_txt}")
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                # candidates[0].content.parts[].text
                for cand in (data.get("candidates") or []):
                    content = cand.get("content") or {}
                    for part in (content.get("parts") or []):
                        t = part.get("text")
                        if t:
                            yield t

    async def embed(self, text: str, model: str = None) -> List[float]:
        """[r226] Gemini 임베딩 — text-embedding-004. (정련소는 Ollama 쓰지만 호환용)"""
        if not _HAS_HTTPX:
            raise RuntimeError("httpx 미설치")
        model = model or "text-embedding-004"
        url = f"{_BASE}/models/{model}:embedContent?key={self.api_key}"
        body = {"model": f"models/{model}", "content": {"parts": [{"text": text}]}}
        r = await self.client.post(url, json=body)
        r.raise_for_status()
        data = r.json()
        return (data.get("embedding") or {}).get("values") or []


_clients: dict = {}


def get_gemini(api_key: str) -> GeminiClient:
    """API 키별 클라이언트 캐시."""
    if api_key not in _clients:
        _clients[api_key] = GeminiClient(api_key)
    return _clients[api_key]
