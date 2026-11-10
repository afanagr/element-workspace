"""Provider construction, preference resolution and automatic fallback."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider, ProviderCapabilities
from app.llm.catalog import EMBEDDING_MODELS, FALLBACK_ORDER, PROVIDERS, ProviderMeta
from app.llm.compatible_provider import CompatibleProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.local_embeddings import LocalEmbeddingProvider
from app.llm.openai_provider import OpenAIProvider
from app.llm.schemas import NoProviderAvailable, ProviderError
from app.logging_config import get_logger
from app.models import AppSetting, ProviderCredential
from app.security import decrypt_secret, fingerprint

logger = get_logger(__name__)

PREFERENCES_KEY = "preferences"


@dataclass(slots=True)
class ResolvedProvider:
    provider: LLMProvider
    provider_name: str
    model: str
    requested: str | None = None
    fallback_from: str | None = None

    @property
    def is_fallback(self) -> bool:
        return bool(self.fallback_from)


@dataclass(slots=True)
class ResolvedEmbedding:
    provider: LLMProvider
    provider_name: str
    model: str


@dataclass(slots=True)
class ProviderView:
    name: str
    label: str
    configured: bool
    enabled: bool
    source: str
    models: list[str]
    default_model: str
    base_url: str | None
    supports_tools: bool
    supports_vision: bool
    supports_embeddings: bool
    local: bool
    api_key_masked: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "configured": self.configured,
            "enabled": self.enabled,
            "source": self.source,
            "models": self.models,
            "default_model": self.default_model,
            "base_url": self.base_url,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "supports_embeddings": self.supports_embeddings,
            "local": self.local,
            "api_key_masked": self.api_key_masked,
        }


def _ollama_v1(base: str) -> str:
    base = (base or "http://ollama:11434").rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


class ProviderRegistry:
    """Builds and caches provider clients from env vars + UI-saved credentials."""

    def __init__(self) -> None:
        self._chat: dict[tuple[str, str, str], LLMProvider] = {}
        self._local: LocalEmbeddingProvider | None = None
        self._ollama_cache: tuple[float, bool] | None = None

    # ---------------------------------------------------------- lifecycle
    def invalidate(self) -> None:
        self._chat.clear()
        self._local = None
        self._ollama_cache = None

    async def local_available(self, name: str) -> bool:
        """Ollama only counts as configured when its API actually answers."""
        if name != "ollama":
            return True
        now = time.monotonic()
        if self._ollama_cache is not None and now - self._ollama_cache[0] < 15:
            return self._ollama_cache[1]
        base = (settings.ollama_base_url or "http://ollama:11434").rstrip("/")
        reachable = False
        try:
            import httpx

            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{base}/api/tags")
                reachable = response.status_code == 200
        except Exception:  # noqa: BLE001 - unreachable is a valid state
            reachable = False
        self._ollama_cache = (now, reachable)
        return reachable


    # --------------------------------------------------------- credentials
    async def credential_info(self, db: AsyncSession, name: str) -> tuple[str, str | None, str | None, bool]:
        """Return (api_key, base_url, default_model, enabled) with DB > env precedence."""
        meta = PROVIDERS[name]
        credential = await db.get(ProviderCredential, name)
        key = decrypt_secret(credential.api_key_enc) if credential else ""
        base_url = (credential.base_url if credential else None) or None
        default_model = (credential.default_model if credential else None) or None
        enabled = credential.enabled if credential else True
        if not key:
            key = settings.provider_api_key(name)
        if not base_url:
            if name == "openai" and settings.openai_base_url:
                base_url = settings.openai_base_url
            elif name == "ollama":
                base_url = _ollama_v1(settings.ollama_base_url)
            else:
                base_url = meta.base_url
        return key, base_url, default_model, enabled

    def _default_model_for(self, meta: ProviderMeta, db_default: str | None) -> str:
        if db_default:
            return db_default
        if settings.llm_provider == meta.name and settings.llm_model:
            return settings.llm_model
        if meta.name == "ollama" and settings.ollama_model:
            return settings.ollama_model
        return meta.default_model

    async def is_configured(self, db: AsyncSession, name: str) -> bool:
        meta = PROVIDERS.get(name)
        if meta is None:
            return False
        key, _, _, enabled = await self.credential_info(db, name)
        if not enabled:
            return False
        if meta.local:
            return await self.local_available(name)
        return bool(key)

    async def get_provider(self, db: AsyncSession, name: str) -> LLMProvider | None:
        meta = PROVIDERS.get(name)
        if meta is None:
            return None
        key, base_url, db_default, enabled = await self.credential_info(db, name)
        if not enabled or (not key and not meta.local):
            return None
        default_model = self._default_model_for(meta, db_default)
        cache_key = (name, fingerprint(key + (base_url or "")), default_model)
        cached = self._chat.get(cache_key)
        if cached is not None:
            return cached

        try:
            if name == "openai":
                provider: LLMProvider = OpenAIProvider(
                    api_key=key,
                    base_url=base_url,
                    models=meta.models,
                    default_model=default_model,
                    capabilities=ProviderCapabilities(
                        supports_tools=True, supports_vision=True, supports_embeddings=True
                    ),
                )
                provider.default_embed_model = settings.embedding_model  # type: ignore[attr-defined]
            elif name == "anthropic":
                provider = AnthropicProvider(api_key=key, default_model=default_model)
            elif name == "gemini":
                provider = GeminiProvider(api_key=key, default_model=default_model)
            else:
                provider = CompatibleProvider(
                    meta, api_key=key, base_url=base_url, default_model=default_model
                )
        except ProviderError as exc:
            logger.warning("Provider %s could not be initialised: %s", name, exc.message)
            return None
        except Exception as exc:  # noqa: BLE001 - never let one provider break the app
            logger.warning("Provider %s initialisation error: %s", name, exc)
            return None

        self._chat[cache_key] = provider
        return provider

    # --------------------------------------------------------- preferences
    async def preferences(self, db: AsyncSession) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
        }
        row = await db.get(AppSetting, PREFERENCES_KEY)
        if row is not None and isinstance(row.value, dict):
            for key, value in row.value.items():
                if value not in (None, ""):
                    defaults[key] = value
        return defaults

    async def save_preferences(self, db: AsyncSession, values: dict[str, Any]) -> dict[str, Any]:
        current = await self.preferences(db)
        current.update({key: value for key, value in values.items() if value is not None})
        row = await db.get(AppSetting, PREFERENCES_KEY)
        if row is None:
            row = AppSetting(key=PREFERENCES_KEY, value=current)
            db.add(row)
        else:
            row.value = dict(current)
        await db.flush()
        self.invalidate()
        return current

    # -------------------------------------------------------------- chat
    async def resolve(
        self,
        db: AsyncSession,
        preferred: str | None = None,
        model: str | None = None,
    ) -> ResolvedProvider:
        prefs = await self.preferences(db)
        requested = preferred or str(prefs.get("provider") or settings.llm_provider)
        order: list[str] = []
        for candidate in (requested, str(prefs.get("provider") or ""), settings.llm_provider):
            if candidate and candidate not in order:
                order.append(candidate)
        for candidate in FALLBACK_ORDER:
            if candidate not in order:
                order.append(candidate)

        attempted: list[dict[str, str]] = []
        for index, name in enumerate(order):
            if not await self.is_configured(db, name):
                attempted.append({"provider": name, "reason": "no API key or unreachable"})
                continue
            provider = await self.get_provider(db, name)
            if provider is None:
                attempted.append({"provider": name, "reason": "no API key configured"})
                continue
            if index == 0:
                per_provider_model = prefs.get("model") if name == prefs.get("provider") else None
                chosen = model or per_provider_model or provider.default_model
            else:
                chosen = provider.default_model
            fallback_from = requested if name != requested else None
            if fallback_from:
                logger.info("Falling back from %s to %s (%s)", requested, name, chosen)
            return ResolvedProvider(
                provider=provider,
                provider_name=name,
                model=chosen,
                requested=requested,
                fallback_from=fallback_from,
            )

        raise NoProviderAvailable(
            "No LLM provider is configured. Add an API key in Settings or start the local "
            "Ollama profile (`docker compose --profile local-llm up -d`).",
            details={"attempted": attempted, "requested": requested},
        )

    async def candidates(
        self, db: AsyncSession, preferred: str | None = None, model: str | None = None
    ) -> list[ResolvedProvider]:
        """Every usable provider in fallback order (used by the agent on 429/5xx)."""
        prefs = await self.preferences(db)
        requested = preferred or str(prefs.get("provider") or settings.llm_provider)
        order: list[str] = []
        for candidate in (requested, *FALLBACK_ORDER):
            if candidate and candidate not in order:
                order.append(candidate)
        resolved: list[ResolvedProvider] = []
        for name in order:
            if not await self.is_configured(db, name):
                continue
            provider = await self.get_provider(db, name)
            if provider is None:
                continue
            chosen = model if name == requested and model else provider.default_model
            resolved.append(
                ResolvedProvider(
                    provider=provider,
                    provider_name=name,
                    model=chosen,
                    requested=requested,
                    fallback_from=requested if name != requested else None,
                )
            )
        if not resolved:
            raise NoProviderAvailable("No LLM provider is configured.", details={"requested": requested})
        return resolved

    # -------------------------------------------------------- embeddings
    async def resolve_embeddings(
        self,
        db: AsyncSession,
        preferred: str | None = None,
        model: str | None = None,
    ) -> ResolvedEmbedding:
        prefs = await self.preferences(db)
        order: list[str] = []
        for candidate in (
            preferred,
            prefs.get("embedding_provider"),
            settings.embedding_provider,
            "openai",
            "gemini",
            "local",
        ):
            name = str(candidate or "")
            if name and name not in order:
                order.append(name)

        attempted: list[dict[str, str]] = []
        for name in order:
            if name == "local":
                if self._local is None:
                    self._local = LocalEmbeddingProvider(settings.local_embedding_model)
                return ResolvedEmbedding(self._local, "local", model or settings.local_embedding_model)
            provider = await self.get_provider(db, name)
            if provider is None or not provider.supports_embeddings:
                attempted.append({"provider": name, "reason": "not configured or no embedding support"})
                continue
            resolved_model = model
            if not resolved_model and prefs.get("embedding_provider") == name:
                resolved_model = str(prefs.get("embedding_model") or "") or None
            if not resolved_model:
                if name == settings.embedding_provider:
                    resolved_model = settings.embedding_model
                elif name == "ollama":
                    resolved_model = "nomic-embed-text"
                else:
                    resolved_model = (EMBEDDING_MODELS.get(name) or (provider.default_model,))[0]
            return ResolvedEmbedding(provider, name, resolved_model)

        raise NoProviderAvailable(
            "No embedding provider is available. Document search / RAG is disabled until you "
            "configure an OpenAI/Gemini key or enable the local embedding model.",
            details={"attempted": attempted},
        )

    # -------------------------------------------------------------- views
    async def list_providers(self, db: AsyncSession) -> list[ProviderView]:
        views: list[ProviderView] = []
        for name, meta in PROVIDERS.items():
            credential = await db.get(ProviderCredential, name)
            key, base_url, db_default, enabled = await self.credential_info(db, name)
            db_key = bool(credential and credential.api_key_enc)
            env_key = bool(settings.provider_api_key(name)) and not meta.local
            if meta.local:
                configured = await self.local_available(name)
            else:
                configured = bool(key)
            source = "database" if db_key else ("environment" if env_key else ("local" if meta.local else "none"))
            views.append(
                ProviderView(
                    name=name,
                    label=meta.label,
                    configured=configured,
                    enabled=enabled,
                    source=source,
                    models=list(meta.models),
                    default_model=self._default_model_for(meta, db_default),
                    base_url=base_url,
                    supports_tools=meta.supports_tools,
                    supports_vision=meta.supports_vision,
                    supports_embeddings=meta.supports_embeddings,
                    local=meta.local,
                    api_key_masked="" if meta.local else mask_for(key),
                )
            )
        return views


def mask_for(key: str) -> str:
    from app.security import mask_secret

    return mask_secret(key)


registry = ProviderRegistry()



