"""[r226] LLM 라우터 — 모델명으로 Ollama / Gemini 자동 선택.

순환 import 방지를 위해 최상위 모듈로 분리. main/refinery/mindmap 모두 여기서 import.
ollama_client, gemini_client 만 의존.

- model 이 'gemini' 로 시작 → Gemini(전역 API 키), 아니면 Ollama.
- 반환 객체는 chat_stream(messages, model, temperature) 동일 인터페이스.
- 임베딩(embed)은 항상 Ollama 권장 (인덱싱 일관성) — 호출처가 get_ollama 직접 사용.
"""
from typing import Optional, Dict, Any

from ollama_client import get_ollama

try:
    from gemini_client import get_gemini, FREE_TIER_MODELS as GEMINI_FREE_MODELS
    HAS_GEMINI = True
except Exception:
    HAS_GEMINI = False
    GEMINI_FREE_MODELS = []


# 프로세스 전역 Gemini 설정 — 사용자가 발급한 무료 등급 API 키.
GEMINI_CONFIG: Dict[str, Any] = {"api_key": None, "label": None}


def is_gemini_model(model: Optional[str]) -> bool:
    return bool(model) and model.lower().startswith("gemini")


def get_llm(model: Optional[str] = None):
    """모델명으로 Ollama / Gemini 자동 선택.

    [r277] model 미지정 시 Gemini 키가 있으면 Gemini 우선(Ollama 미설치 환경 대비).
    """
    if is_gemini_model(model):
        if not HAS_GEMINI:
            raise RuntimeError("Gemini 모듈 미설치 (httpx 필요)")
        key = GEMINI_CONFIG.get("api_key")
        if not key:
            raise RuntimeError("Gemini API 키 미설정 — AI Agent 페이지에서 등록하세요")
        return get_gemini(key)
    # 모델 미지정 + Gemini 키 등록돼 있으면 Gemini 자동 우선
    if not model and HAS_GEMINI and GEMINI_CONFIG.get("api_key"):
        return get_gemini(GEMINI_CONFIG["api_key"])
    return get_ollama()


def llm_status() -> Dict[str, Any]:
    """[r277] 현재 LLM 가용 상태 — health 응답·에러 안내용."""
    return {
        "gemini_registered": bool(HAS_GEMINI and GEMINI_CONFIG.get("api_key")),
        "gemini_label": GEMINI_CONFIG.get("label"),
        "has_gemini_module": HAS_GEMINI,
    }
