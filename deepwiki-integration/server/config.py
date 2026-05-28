"""환경 변수 로드 + 설정 검증."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    """Deep Wiki 백엔드 설정. .env에서 자동 로드."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Supabase
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # Ollama
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    LLM_MODEL: str = "qwen2.5-coder:14b"
    EMBED_MODEL: str = "nomic-embed-text"

    # 서버
    PORT: int = 8000
    CORS_ORIGINS: str = "https://namkuri.github.io,http://localhost:3000"

    # 인덱싱
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50
    TOP_K: int = 8
    GIT_CLONE_DIR: str = "./repos"
    MAX_FILE_SIZE_KB: int = 500

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()  # 모듈 로드 시 한 번만
