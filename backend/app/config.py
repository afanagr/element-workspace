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
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000"

    # ---------------------------------------------------------------- auth
    # Bootstrap administrator, created on first start when no user exists yet.
    admin_username: str = "admin"
    admin_password: str = "admin"
    session_ttl_hours: int = 24 * 14

    # ----------------------------------------------------------- persistence
    database_url: str = "postgresql+asyncpg://workspace:workspace@localhost:5432/workspace"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "workspace_documents"
    workspace_dir: Path = Path("/data/workspace")

    # ---------------------------------------------------------------- llm
    # Chat providers: DeepSeek (cloud) plus Qwen in two flavours - the cloud
    # DashScope API and a local OpenAI-compatible server (Ollama, vLLM, ...).
    # Individual accounts normally get their own key from an administrator; the
    # values below are deployment-wide fallbacks and endpoint overrides.
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-chat"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"

    # Qwen cloud (Alibaba Cloud DashScope, OpenAI-compatible mode).
    qwen_api_key: str = ""
    # Accepted alias for deployments that already export DASHSCOPE_API_KEY.
    dashscope_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"

    # Local Qwen: leave QWEN_LOCAL_BASE_URL empty to disable the provider.
    # Ollama: http://localhost:11434/v1 (inside Docker: http://host.docker.internal:11434/v1)
    # vLLM/LM Studio: http://localhost:8000/v1
    qwen_local_base_url: str = ""
    qwen_local_api_key: str = ""
    qwen_local_model: str = "qwen2.5:7b-instruct"

    llm_request_timeout: float = 180.0
    llm_max_retries: int = 2
    max_agent_steps: int = 8

    # ---------------------------------------------------------- embeddings
    # DeepSeek/Qwen chat endpoints expose no embedding API in this setup, so
    # vectors are always computed locally.
    embedding_provider: str = "local"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
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

    # ------------------------------------------------------------------- ocr
    # Scanned PDFs are rasterised per page and recognised with Tesseract.
    # Text pages are left untouched, so normal PDFs do not pay the OCR cost.
    ocr_pdf_enabled: bool = True
    ocr_lang: str = ""  # empty -> tesseract_lang
    ocr_dpi: int = 300
    ocr_max_pages: int = 200
    ocr_min_page_chars: int = 20
    ocr_timeout_seconds: int = 120
    # Total OCR budget per document: keeps one huge scan from blocking a request
    # (or a Celery worker) for longer than the proxy timeout.
    ocr_budget_seconds: int = 540
    ocr_deskew: bool = True
    ocr_min_orientation_confidence: float = 1.0

    # --------------------------------------------------------- onlyoffice
    # OnlyOffice Document Server replaces the built-in editors when configured.
    # Set ONLYOFFICE_ENABLED=false (or clear the URL/secret) to fall back to the
    # legacy in-app viewers; the previous implementation is kept in the repo.
    onlyoffice_enabled: bool = False
    onlyoffice_url: str = ""  # public URL the browser loads api.js from
    onlyoffice_jwt_secret: str = ""
    # Base URL the Document Server container uses to reach this API
    # (downloads and callbacks are proxied inside the compose network).
    onlyoffice_api_url: str = "http://backend:8000"
    onlyoffice_internal_url: str = "http://documentserver"
    onlyoffice_convert_enabled: bool = True
    onlyoffice_convert_timeout: int = 120
    onlyoffice_lang: str = "ru"
    onlyoffice_allowed_hosts: str = "documentserver,localhost,127.0.0.1"
    onlyoffice_download_ttl: int = 900
    onlyoffice_callback_ttl: int = 86_400

    @property
    def office_configured(self) -> bool:
        return bool(
            self.onlyoffice_enabled and self.onlyoffice_url and self.onlyoffice_jwt_secret
        )

    @property
    def office_conversion_available(self) -> bool:
        return bool(self.office_configured and self.onlyoffice_convert_enabled)

    @property
    def onlyoffice_hosts(self) -> set[str]:
        return {host.strip().lower() for host in self.onlyoffice_allowed_hosts.split(",") if host.strip()}

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

    @property
    def admin_password_is_default(self) -> bool:
        return self.admin_password in {"", "admin", "change-me", "changeme"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
