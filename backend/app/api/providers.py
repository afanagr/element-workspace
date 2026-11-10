"""Provider catalogue, connection tests and encrypted credential management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import current_user
from app.llm.catalog import EMBEDDING_MODELS, EMBEDDING_PROVIDERS, PROVIDERS
from app.llm.factory import registry
from app.llm.local_embeddings import local_embeddings_available
from app.llm.schemas import NoProviderAvailable, ProviderError
from app.logging_config import get_logger
from app.models import ProviderCredential
from app.schemas import CredentialIn, PreferencesIn, ProviderTestOut
from app.security import encrypt_secret
from app.services.embeddings import EmbeddingService

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["providers"])
CurrentUser = Depends(current_user)


def _embedding_options(views: list[Any]) -> list[dict[str, Any]]:
    configured = {view.name: view.configured for view in views}
    local_ready = local_embeddings_available()
    options: list[dict[str, Any]] = []
    for name in EMBEDDING_PROVIDERS:
        available = local_ready if name == "local" else configured.get(name, False)
        options.append(
            {
                "name": name,
                "label": {
                    "openai": "OpenAI",
                    "gemini": "Google Gemini",
                    "ollama": "Ollama (local)",
                    "local": "Local sentence-transformers",
                }.get(name, name.title()),
                "configured": bool(available),
                "models": list(EMBEDDING_MODELS.get(name, ())),
            }
        )
    return options


@router.get("/providers")
async def list_providers(db: AsyncSession = Depends(get_db)) -> dict:
    views = await registry.list_providers(db)
    prefs = await registry.preferences(db)
    return {
        "providers": [view.to_dict() for view in views],
        "preferences": prefs,
        "embedding_providers": _embedding_options(views),
        "chat_available": any(view.configured for view in views),
        "degraded_banner": None
        if any(view.configured for view in views)
        else "No LLM provider configured - add a key in Settings or start Ollama.",
    }


@router.post("/providers/{provider}/test", response_model=ProviderTestOut)
async def test_provider(provider: str, db: AsyncSession = Depends(get_db)) -> ProviderTestOut:
    provider = provider.lower()
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")
    instance = await registry.get_provider(db, provider)
    if instance is None:
        return ProviderTestOut(
            provider=provider,
            ok=False,
            message="Not configured: add an API key first"
            + (" and start the ollama profile" if provider == "ollama" else ""),
        )
    health = await instance.health()
    return ProviderTestOut(
        provider=provider,
        ok=health.ok,
        message=health.message,
        latency_ms=health.latency_ms,
        models=health.models,
    )


@router.get("/providers/{provider}/models")
async def provider_models(provider: str, db: AsyncSession = Depends(get_db)) -> dict:
    provider = provider.lower()
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")
    instance = await registry.get_provider(db, provider)
    if instance is None:
        return {"models": list(PROVIDERS[provider].models), "dynamic": False}
    try:
        models = await instance.list_models()
        return {"models": models, "dynamic": True}
    except ProviderError as exc:
        return {
            "models": list(PROVIDERS[provider].models),
            "dynamic": False,
            "error": exc.message,
        }


@router.post("/embeddings/test")
async def test_embeddings(
    provider: str | None = None,
    model: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        resolved = await registry.resolve_embeddings(db, preferred=provider, model=model)
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
async def get_settings(db: AsyncSession = Depends(get_db)) -> dict:
    views = await registry.list_providers(db)
    prefs = await registry.preferences(db)
    return {
        "providers": [view.to_dict() for view in views],
        "preferences": prefs,
        "embedding_providers": _embedding_options(views),
        "secret_key_insecure": settings.secret_key_is_insecure,
        "max_upload_mb": settings.max_upload_mb,
        "default_provider": settings.llm_provider,
        "default_model": settings.llm_model,
        "ollama_base_url": settings.ollama_base_url,
        "auth_enabled": bool(settings.app_user_token),
    }


@router.put("/settings/credentials")
async def save_credential(
    payload: CredentialIn,
    db: AsyncSession = Depends(get_db),
    user: str = CurrentUser,
) -> dict:
    provider = payload.provider
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")

    credential = await db.get(ProviderCredential, provider)
    if credential is None:
        credential = ProviderCredential(provider=provider)
        db.add(credential)
    if payload.api_key is not None:
        value = payload.api_key.strip()
        credential.api_key_enc = encrypt_secret(value) if value else None
    if payload.base_url is not None:
        credential.base_url = payload.base_url.strip() or None
    if payload.default_model is not None:
        credential.default_model = payload.default_model.strip() or None
    if payload.enabled is not None:
        credential.enabled = payload.enabled

    await db.commit()
    registry.invalidate()

    views = await registry.list_providers(db)
    view = next((item for item in views if item.name == provider), None)
    return {"saved": True, "provider": view.to_dict() if view else {}}


@router.put("/settings/preferences")
async def save_preferences(
    payload: PreferencesIn,
    db: AsyncSession = Depends(get_db),
    user: str = CurrentUser,
) -> dict:
    values = payload.model_dump(exclude_none=True)
    provider = values.get("provider")
    if provider and provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    embedding_provider = values.get("embedding_provider")
    if embedding_provider and embedding_provider not in EMBEDDING_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown embedding provider: {embedding_provider}")

    preferences = await registry.save_preferences(db, values)
    await db.commit()
    return {
        "preferences": preferences,
        "reindex_required": bool(
            {"embedding_provider", "embedding_model"} & set(values.keys())
        ),
    }

