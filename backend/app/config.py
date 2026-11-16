"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    # Chat models are NOT configured here: every account owns a list of
    # OpenAI-compatible connections (name, base URL, key, model) managed by an
    # administrator in /admin -> Users (see ``app.llm.factory``).
    llm_request_timeout: float = 180.0
    llm_max_retries: int = 2
    # Some gateways reject the unknown ``stream_options`` field: turn it off there.
    llm_stream_options: bool = True
    max_agent_steps: int = 8
    # Hard limits of one agent run: wall clock and estimated context size.
    agent_deadline_seconds: float = 300.0
    agent_token_budget: int = 48_000
    agent_history_tokens: int = 12_000
    # Per-tool timeout: one slow conversion must not hold the stream forever.
    tool_timeout_seconds: float = 150.0
    # Stop the loop when the same failing call repeats this many times.
    tool_repeat_limit: int = 2

    # ---------------------------------------------------------- embeddings
    # Vectors are always computed locally: the chat endpoints we talk to expose
    # no embedding API.
    local_embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_batch_size: int = 32

    # ----------------------------------------------------------- retrieval
    chunk_size: int = 1200
    chunk_overlap: int = 200
    rag_top_k: int = 6
    # Fragments below this cosine score are dropped (0 disables the filter).
    rag_score_threshold: float = 0.2
    # Keep at most N fragments of one document so a single file cannot fill the context.
    rag_max_per_document: int = 2

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

    # ------------------------------------------------------- ocr quality
    # LSTM engine only (oem 1) is the most accurate for modern models.
    ocr_oem: int = 1
    # Page segmentation modes tried per page; the result with the highest mean
    # word confidence wins (3 = auto, 6 = single uniform block, 4 = single column).
    ocr_psm: str = "3,6"
    ocr_binarize: bool = True
    ocr_denoise: bool = True
    ocr_border_px: int = 24
    # Fine skew search range in degrees (0 disables the projection-profile pass).
    ocr_deskew_angle: float = 6.0
    # Below this mean confidence the page is retried with English-only models.
    ocr_min_confidence: float = 55.0
    # Bump to invalidate cached OCR results after tuning the settings above.
    ocr_cache_version: int = 2

    # --------------------------------------------------------- onlyoffice
    # Office documents are edited (and converted to PDF) by the OnlyOffice
    # Document Server. Set ONLYOFFICE_ENABLED=false (or clear the URL/secret) to
    # disable the editor; such files then fall back to the read-only preview.
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
    # Long enough for an editor left open for days; the token is HMAC-signed and
    # bound to one node and one user.
    onlyoffice_callback_ttl: int = 7 * 86_400

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
    def ocr_psm_list(self) -> list[int]:
        """Page segmentation modes to try, in order (invalid values are ignored)."""
        modes: list[int] = []
        for part in (self.ocr_psm or "").split(","):
            value = part.strip()
            if value.isdigit() and 0 <= int(value) <= 13:
                modes.append(int(value))
        return modes or [3, 6]

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
