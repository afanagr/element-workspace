"""Health check: liveness plus configuration readiness for the UI banner."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import settings
from app.db import get_db
from app.llm.catalog import PROVIDERS
from app.llm.factory import DEFAULT_PROVIDER, registry
from app.llm.local_embeddings import local_embeddings_available
from app.logging_config import get_logger
from app.services.vectorstore import vector_store

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["health"])


async def _check_database(db: AsyncSession) -> tuple[bool, str]:
    try:
        await db.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


async def _check_redis() -> tuple[bool, str]:
    try:
        import redis.asyncio as redis_async

        client = redis_async.from_url(settings.redis_url, socket_timeout=3)
        try:
            await client.ping()
        finally:
            await client.aclose()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    """Public readiness probe (used by docker healthchecks and the UI banner).

    Per-account key state is *not* exposed here - it comes from ``/api/providers``
    for authenticated users.
    """
    db_ok, db_message = await _check_database(db)
    qdrant_ok, qdrant_message = await vector_store.health()
    redis_ok, redis_message = await _check_redis()

    embedding_configured = ["local"] if local_embeddings_available() else []
    views = await registry.list_providers(db, None)
    prefs = await registry.preferences(db)
    configured = [view.name for view in views if view.configured]
    default_provider = str(prefs.get("provider") or DEFAULT_PROVIDER)
    default_model = str(prefs.get("model") or settings.llm_model)

    warnings: list[str] = []
    banner: str | None = None
    if settings.secret_key_is_insecure:
        warnings.append(
            "APP_SECRET_KEY is unset/insecure: stored provider keys are weakly encrypted. "
            "Set a long random value in .env."
        )
    if settings.admin_password_is_default:
        warnings.append(
            "The bootstrap administrator still uses the default password - change it after login."
        )
    if not configured:
        warnings.append(
            "No chat provider is configured: no DeepSeek/Qwen API key is set and "
            "QWEN_LOCAL_BASE_URL is empty."
        )
        banner = "No chat provider is configured - administrators can assign keys in /admin."
    if not embedding_configured:
        warnings.append(
            "Local embeddings are unavailable - document search (RAG) is disabled. "
            "Install sentence-transformers (INSTALL_LOCAL_EMBEDDINGS=1)."
        )
    if not qdrant_ok:
        warnings.append(f"Vector database unavailable: {qdrant_message}")
    if not redis_ok:
        warnings.append(f"Redis unavailable: {redis_message} (Celery jobs will run inline)")

    status = "ok"
    if not db_ok:
        status = "error"
    elif warnings or not qdrant_ok:
        status = "degraded"

    return {
        "status": status,
        "version": __version__,
        "checks": {
            "database": {"ok": db_ok, "message": db_message},
            "qdrant": {"ok": qdrant_ok, "message": qdrant_message},
            "redis": {"ok": redis_ok, "message": redis_message},
        },
        "providers": {
            "configured": configured,
            "embedding_configured": embedding_configured,
            "default_provider": default_provider if default_provider in PROVIDERS else DEFAULT_PROVIDER,
            "default_model": default_model,
            "details": [view.to_dict() for view in views],
            "auth": "required",
        },
        "banner": banner,
        "warnings": warnings,
    }
