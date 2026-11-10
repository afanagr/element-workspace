"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_SECRETS = {"", "change-me", "change-me-please-use-a-long-random-string", "dev-insecure-secret-change-me"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "Element Workspace"
    app_secret_key: str = "dev-insecure-secret-change-me"
    app_user_token: str = ""
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000"

    # ----------------------------------------------------------- persistence
    database_url: str = "postgresql+asyncpg://workspace:workspace@localhost:5432/workspace"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "workspace_documents"
    workspace_dir: Path = Path("/data/workspace")

    # ---------------------------------------------------------------- llm
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"
    openai_api_key: str = ""
    openai_base_url: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    deepseek_api_key: str = ""
    openrouter_api_key: str = ""
    groq_api_key: str = ""
    together_api_key: str = ""
    xai_api_key: str = ""
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.2"
    llm_request_timeout: float = 180.0
    llm_max_retries: int = 2
    max_agent_steps: int = 8

    # ---------------------------------------------------------- embeddings
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    local_embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_batch_size: int = 32

    # ----------------------------------------------------------- retrieval
    chunk_size: int = 1200
    chunk_overlap: int = 200
    rag_top_k: int = 6

    # -------------------------------------------------------------- limits
    max_upload_mb: int = 200
    max_extract_files: int = 2000
    max_extract_mb: int = 500
    max_text_preview_chars: int = 60_000
    max_attachment_chars: int = 12_000

    tesseract_lang: str = "eng+rus"
    tesseract_cmd: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def secret_key_is_insecure(self) -> bool:
        return self.app_secret_key in INSECURE_SECRETS or len(self.app_secret_key) < 16

    @property
    def blob_dir(self) -> Path:
        return Path(self.workspace_dir) / "blobs"

    @property
    def temp_dir(self) -> Path:
        return Path(self.workspace_dir) / "tmp"

    def provider_api_key(self, provider: str) -> str:
        return {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "gemini": self.google_api_key,
            "deepseek": self.deepseek_api_key,
            "openrouter": self.openrouter_api_key,
            "groq": self.groq_api_key,
            "together": self.together_api_key,
            "xai": self.xai_api_key,
            "ollama": "ollama",
        }.get(provider, "")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
