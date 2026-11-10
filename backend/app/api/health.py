"""Health check: liveness plus configuration readiness for the UI banner."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import settings
from app.db import get_db
from app.llm.factory import registry
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
    db_ok, db_message = await _check_database(db)
    qdrant_ok, qdrant_message = await vector_store.health()
    redis_ok, redis_message = await _check_redis()

    providers: list[dict] = []
    configured: list[str] = []
    embedding_configured: list[str] = []
    if db_ok:
        try:
            views = await registry.list_providers(db)
            providers = [view.to_dict() for view in views]
            configured = [view.name for view in views if view.configured and not view.local]
            embedding_configured = [view.name for view in views if view.configured and view.supports_embeddings]
            if local_embeddings_available():
                embedding_configured.append("local")
            if any(view.local and view.configured for view in views):
                configured.append("ollama")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Provider introspection failed: %s", exc)

    warnings: list[str] = []
    banner: str | None = None
    if settings.secret_key_is_insecure:
        warnings.append(
            "APP_SECRET_KEY is unset/insecure: saved provider keys are weakly encrypted. "
            "Set a long random value in .env."
        )
    if not configured:
        banner = (
            "No LLM provider is configured. Add an API key in Settings (⚙) or start the local "
            "Ollama profile: docker compose --profile local-llm up -d && docker compose exec ollama ollama pull llama3.2"
        )
    elif "ollama" in configured and not [item for item in configured if item != "ollama"]:
        banner = (
            "Running in local mode with Ollama. Pull a model first: "
            "docker compose exec ollama ollama pull llama3.2"
        )
    if not embedding_configured:
        warnings.append(
            "No embedding provider available - document search (RAG) is disabled until an "
            "OpenAI/Gemini key is configured or EMBEDDING_PROVIDER=local."
        )
    if not qdrant_ok:
        warnings.append(f"Vector database unavailable: {qdrant_message}")
    if not redis_ok:
        warnings.append(f"Redis unavailable: {redis_message} (Celery jobs will run inline)")

    status = "ok"
    if not db_ok:
        status = "error"
    elif not configured or not qdrant_ok or warnings:
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
            "default_provider": settings.llm_provider,
            "default_model": settings.llm_model,
            "details": providers,
        },
        "banner": banner,
        "warnings": warnings,
    }
