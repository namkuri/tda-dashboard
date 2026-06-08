"""[r226] LLM 라우터 — 모델명으로 Ollama / Gemini 자동 선택.

순환 import 방지를 위해 최상위 모듈로 분리. main/refinery/mindmap 모두 여기서 import.
ollama_client, gemini_client 만 의존.

- model 이 'gemini' 로 시작 → Gemini(전역 API 키), 아니면 Ollama.
- 반환 객체는 chat_stream(messages, model, temperature) 동일 인터페이스.
- 임베딩(embed)은 항상 Ollama 권장 (인덱싱 일관성) — 호출처가 get_ollama 직접 사용.

[r296] Gemini 키 영속화 — 백엔드 재시작 시 휘발되던 문제 해결.
  · 파일: 서버 디렉터리의 .gemini_config.json (gitignore 권장)
  · 환경변수 폴백: GEMINI_API_KEY (.env 등)
  · 등록/삭제 시 자동 저장.
"""
import json
import os
from pathlib import Path
from typing import Optional, Dict, Any

from ollama_client import get_ollama

try:
    from gemini_client import get_gemini, FREE_TIER_MODELS as GEMINI_FREE_MODELS
    HAS_GEMINI = True
except Exception:
    HAS_GEMINI = False
    GEMINI_FREE_MODELS = []


# [r296] 영속화 파일 — llm_router.py 옆(서버 디렉터리). gitignore 처리 권장.
_CONFIG_FILE = Path(__file__).parent / ".gemini_config.json"

# 프로세스 전역 Gemini 설정 — 사용자가 발급한 무료 등급 API 키.
GEMINI_CONFIG: Dict[str, Any] = {"api_key": None, "label": None}


def save_gemini_config() -> bool:
    """[r296] 현재 GEMINI_CONFIG 를 파일에 저장. 등록/삭제 라우트에서 호출."""
    try:
        if GEMINI_CONFIG.get("api_key"):
            data = {"api_key": GEMINI_CONFIG.get("api_key"), "label": GEMINI_CONFIG.get("label")}
            _CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        else:
            if _CONFIG_FILE.exists():
                _CONFIG_FILE.unlink()
        return True
    except Exception as e:
        print(f"[llm_router] ⚠ Gemini 키 파일 저장 실패: {e}")
        return False


def load_gemini_config() -> Dict[str, Any]:
    """[r296] startup 시 호출 — 파일 우선, env(GEMINI_API_KEY) 폴백."""
    info = {"source": None, "key_present": False}
    try:
        if _CONFIG_FILE.exists():
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            key = (data.get("api_key") or "").strip()
            if key:
                GEMINI_CONFIG["api_key"] = key
                GEMINI_CONFIG["label"] = data.get("label") or "내 Gemini"
                info.update({"source": "file", "key_present": True,
                             "label": GEMINI_CONFIG["label"],
                             "masked": "..." + key[-4:]})
                return info
    except Exception as e:
        print(f"[llm_router] ⚠ Gemini 키 파일 로드 실패: {e}")
    env_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if env_key:
        GEMINI_CONFIG["api_key"] = env_key
        GEMINI_CONFIG["label"] = os.environ.get("GEMINI_LABEL") or "env Gemini"
        info.update({"source": "env", "key_present": True,
                     "label": GEMINI_CONFIG["label"],
                     "masked": "..." + env_key[-4:]})
    return info


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
