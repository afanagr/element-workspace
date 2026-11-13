"""Model connections of the signed-in account: status, tests and model lists."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import current_user
from app.llm.catalog import EMBEDDING_PROVIDERS
from app.llm.factory import registry
from app.llm.local_embeddings import local_embeddings_available
from app.llm.schemas import NoProviderAvailable, ProviderError
from app.logging_config import get_logger
from app.models import User
from app.schemas import ProviderTestOut
from app.services import users as user_service
from app.services.embeddings import EmbeddingService

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["providers"])
CurrentUser = Depends(current_user)


def _degraded_banner(configured: bool) -> str | None:
    if configured:
        return None
    return (
        "No models are connected to your account. Ask an administrator to add a "
        "connection (name, address, key, model) in /admin → Users."
    )


def _test_payload(name: str, health: Any, models: list[str]) -> ProviderTestOut:
    return ProviderTestOut(
        provider=name,
        ok=health.ok,
        message=health.message,
        latency_ms=health.latency_ms,
        models=health.models or models,
    )


@router.get("/providers")
async def list_providers(
    db: AsyncSession = Depends(get_db), user: User = CurrentUser
) -> dict:
    views = await registry.list_providers(db, user)
    return {
        "providers": [view.to_dict() for view in views],
        "embedding_providers": await registry.embedding_options(),
        "chat_available": bool(views),
        "degraded_banner": _degraded_banner(bool(views)),
    }


@router.post("/providers/test", response_model=ProviderTestOut)
async def test_provider(
    db: AsyncSession = Depends(get_db), user: User = CurrentUser
) -> ProviderTestOut:
    """Test the first connected model (kept for backwards compatibility)."""
    views = await registry.list_providers(db, user)
    if not views:
        return ProviderTestOut(provider="", ok=False, message=_degraded_banner(False) or "")
    name = views[0].name
    health = await registry.test(db, user, name)
    return _test_payload(name, health, views[0].models)


@router.post("/providers/{provider}/test", response_model=ProviderTestOut)
async def test_named_provider(
    provider: str, db: AsyncSession = Depends(get_db), user: User = CurrentUser
) -> ProviderTestOut:
    view = await registry.provider_view(db, user, provider)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Model connection not found: {provider}")
    health = await registry.test(db, user, provider)
    return _test_payload(view.name, health, view.models)


@router.get("/providers/{provider}/models")
async def named_provider_models(
    provider: str, db: AsyncSession = Depends(get_db), user: User = CurrentUser
) -> dict[str, Any]:
    """The connected model merged with the live model list when available."""
    view = await registry.provider_view(db, user, provider)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Model connection not found: {provider}")
    base = {"provider": view.name, "configured": True}
    client = await registry.get_provider(db, user, provider)
    if client is None:
        return {**base, "models": list(view.models), "dynamic": False}
    try:
        live = await asyncio.wait_for(client.list_models(), timeout=15)
    except (ProviderError, asyncio.TimeoutError) as exc:
        message = getattr(exc, "message", "model listing timed out")
        logger.info("Model listing for %s failed: %s", provider, message)
        return {
            **base,
            "models": list(view.models),
            "dynamic": False,
            "error": message,
        }
    merged = list(dict.fromkeys([*live, *view.models]))
    return {**base, "models": merged, "dynamic": True}


@router.post("/embeddings/test")
async def test_embeddings(
    provider: str | None = None, model: str | None = None
) -> dict[str, Any]:
    """Local embeddings power RAG; the test simply embeds a probe string."""
    if provider and provider not in EMBEDDING_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Embedding provider '{provider}' is not available - only local embeddings are used",
        )
    if not local_embeddings_available():
        return {"ok": False, "message": "sentence-transformers is not installed in this image"}
    try:
        resolved = await registry.resolve_embeddings(preferred=provider, model=model)
        service = EmbeddingService(resolved.provider, resolved.model)
        vector = await service.embed_query("connection test")
        return {
            "ok": True,
            "provider": resolved.provider_name,
            "model": resolved.model,
            "dimension": len(vector),
        }
    except (NoProviderAvailable, ProviderError) as exc:
        return {"ok": False, "message": getattr(exc, "message", str(exc))}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Embedding test failed")
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


@router.get("/settings")
async def get_settings(
    db: AsyncSession = Depends(get_db), user: User = CurrentUser
) -> dict:
    views = await registry.list_providers(db, user)
    connections = await user_service.list_connections(db, user)
    return {
        "providers": [view.to_dict() for view in views],
        "embedding_providers": await registry.embedding_options(),
        "secret_key_insecure": settings.secret_key_is_insecure,
        "max_upload_mb": settings.max_upload_mb,
        "auth_enabled": True,
        "user": user_service.user_to_dict(user, connections=connections),
        "admin_password_is_default": settings.admin_password_is_default,
    }
