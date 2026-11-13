"""Provider status, connectivity tests and per-account chat preferences."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import current_admin, current_user
from app.llm.catalog import EMBEDDING_PROVIDERS, PROVIDERS
from app.llm.factory import DEFAULT_PROVIDER, registry
from app.llm.local_embeddings import local_embeddings_available
from app.llm.schemas import NoProviderAvailable, ProviderError
from app.logging_config import get_logger
from app.models import User
from app.schemas import PreferencesIn, ProviderTestOut
from app.services import users as user_service
from app.services.embeddings import EmbeddingService

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["providers"])
CurrentUser = Depends(current_user)
CurrentAdmin = Depends(current_admin)


def _provider_or_404(provider: str) -> str:
    name = (provider or "").strip().lower()
    if name not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")
    return name


def _degraded_banner(configured: bool) -> str | None:
    if configured:
        return None
    return (
        "No chat provider is configured. Ask an administrator to assign a DeepSeek or Qwen "
        "API key, or set QWEN_LOCAL_BASE_URL to use a local Qwen model."
    )


def _test_payload(provider: str, health: Any, models: list[str]) -> ProviderTestOut:
    return ProviderTestOut(
        provider=provider,
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
    prefs = await registry.preferences(db)
    configured = any(view.configured for view in views)
    return {
        "providers": [view.to_dict() for view in views],
        "preferences": prefs,
        "embedding_providers": await registry.embedding_options(),
        "chat_available": configured,
        "degraded_banner": _degraded_banner(configured),
        "default_provider": DEFAULT_PROVIDER,
    }


@router.post("/providers/test", response_model=ProviderTestOut)
async def test_provider(
    db: AsyncSession = Depends(get_db), user: User = CurrentUser
) -> ProviderTestOut:
    """Test the preferred provider (kept for backwards compatibility)."""
    prefs = await registry.preferences(db)
    name = _provider_or_404(str(prefs.get("provider") or DEFAULT_PROVIDER))
    view = await registry.provider_view(db, user, name)
    if not view.configured:
        return ProviderTestOut(
            provider=name, ok=False, message=registry.missing_message(name), models=view.models
        )
    health = await registry.test(db, user, name)
    return _test_payload(name, health, view.models)


@router.post("/providers/{provider}/test", response_model=ProviderTestOut)
async def test_named_provider(
    provider: str, db: AsyncSession = Depends(get_db), user: User = CurrentUser
) -> ProviderTestOut:
    name = _provider_or_404(provider)
    view = await registry.provider_view(db, user, name)
    if not view.configured:
        return ProviderTestOut(
            provider=name, ok=False, message=registry.missing_message(name), models=view.models
        )
    health = await registry.test(db, user, name)
    return _test_payload(name, health, view.models)


@router.get("/providers/models")
async def provider_models() -> dict[str, Any]:
    meta = PROVIDERS[DEFAULT_PROVIDER]
    return {"provider": DEFAULT_PROVIDER, "models": list(meta.models), "dynamic": False}


@router.get("/providers/{provider}/models")
async def named_provider_models(
    provider: str, db: AsyncSession = Depends(get_db), user: User = CurrentUser
) -> dict[str, Any]:
    """Curated catalogue merged with the live model list when available."""
    name = _provider_or_404(provider)
    meta = PROVIDERS[name]
    view = await registry.provider_view(db, user, name)
    base = {"provider": name, "configured": view.configured}
    if not view.configured:
        return {**base, "models": list(meta.models), "dynamic": False}
    client = await registry.get_provider(db, user, name)
    if client is None:
        return {**base, "models": list(meta.models), "dynamic": False}
    try:
        live = await asyncio.wait_for(client.list_models(), timeout=15)
    except (ProviderError, asyncio.TimeoutError) as exc:
        message = getattr(exc, "message", "model listing timed out")
        logger.info("Model listing for %s failed: %s", name, message)
        return {
            **base,
            "models": list(meta.models),
            "dynamic": False,
            "error": message,
        }
    merged = list(dict.fromkeys([*live, *meta.models]))
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
    prefs = await registry.preferences(db)
    default_provider = str(prefs.get("provider") or DEFAULT_PROVIDER)
    return {
        "providers": [view.to_dict() for view in views],
        "preferences": prefs,
        "embedding_providers": await registry.embedding_options(),
        "secret_key_insecure": settings.secret_key_is_insecure,
        "max_upload_mb": settings.max_upload_mb,
        "default_provider": default_provider,
        "default_model": prefs.get("model")
        or user_service.provider_model(user, default_provider),
        "auth_enabled": True,
        "user": user_service.user_to_dict(user),
        "admin_password_is_default": settings.admin_password_is_default,
    }


@router.put("/settings/preferences")
async def save_preferences(
    payload: PreferencesIn,
    db: AsyncSession = Depends(get_db),
    user: User = CurrentAdmin,
) -> dict:
    values = payload.model_dump(exclude_none=True)
    provider = values.get("provider")
    if provider is not None and provider not in PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider '{provider}'. Available: {', '.join(PROVIDERS)}",
        )
    if values.get("embedding_provider") and values["embedding_provider"] not in EMBEDDING_PROVIDERS:
        raise HTTPException(status_code=400, detail="Only local embeddings are available")
    try:
        preferences = await registry.save_preferences(db, values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return {"preferences": preferences, "reindex_required": False}
