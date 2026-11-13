"""Chat provider registry: construction, per-user keys and local embeddings.

Cloud keys (DeepSeek and Qwen/DashScope) are issued per account by an
administrator; deployment-wide environment variables act as a fallback for
accounts without a personal key. ``qwen-local`` speaks the same OpenAI dialect
against an Ollama/vLLM/LM Studio endpoint and needs no key - it is enabled by
setting ``QWEN_LOCAL_BASE_URL``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.base import LLMProvider, ProviderCapabilities, ProviderHealth
from app.llm.catalog import (
    EMBEDDING_MODELS,
    EMBEDDING_PROVIDERS,
    FALLBACK_ORDER,
    PROVIDERS,
    ProviderMeta,
)
from app.llm.compatible_provider import CompatibleProvider
from app.llm.local_embeddings import LocalEmbeddingProvider, local_embeddings_available
from app.llm.schemas import NoProviderAvailable
from app.logging_config import get_logger
from app.models import AppSetting, User
from app.security import fingerprint, mask_secret
from app.services import users as user_service

logger = get_logger(__name__)

PROVIDER_NAMES: tuple[str, ...] = FALLBACK_ORDER
DEFAULT_PROVIDER: str = (
    settings.llm_provider if settings.llm_provider in PROVIDERS else PROVIDER_NAMES[0]
)
# Backwards-compatible alias: the provider used when nothing else is specified.
PROVIDER_NAME = DEFAULT_PROVIDER
PREFERENCES_KEY = "preferences"
_LOCAL_PLACEHOLDER_KEY = "local-endpoint"


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
    requires_api_key: bool = True
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
            "requires_api_key": self.requires_api_key,
        }


class ProviderRegistry:
    """Builds chat clients for every provider that has credentials."""

    def __init__(self) -> None:
        self._chat: dict[tuple[str, str, str, str | None], LLMProvider] = {}
        self._local: LocalEmbeddingProvider | None = None

    # ---------------------------------------------------------- lifecycle
    def invalidate(self) -> None:
        self._chat.clear()
        self._local = None

    # ----------------------------------------------------------- metadata
    def names(self) -> tuple[str, ...]:
        return PROVIDER_NAMES

    def meta(self, provider: str) -> ProviderMeta:
        name = (provider or "").strip().lower()
        if name not in PROVIDERS:
            raise NoProviderAvailable(
                f"Unknown provider: {provider}", details={"provider": provider}
            )
        return PROVIDERS[name]

    def base_url_for(self, provider: str, user: User | None = None) -> str | None:
        self.meta(provider)
        return user_service.provider_endpoint(user, provider) or None

    # --------------------------------------------------------------- chat
    def api_key_for(self, user: User | None, provider: str = DEFAULT_PROVIDER) -> str:
        return user_service.provider_key(user, provider)

    def is_available(self, user: User | None, provider: str) -> bool:
        self.meta(provider)
        return user_service.provider_is_available(user, provider)

    def model_for(
        self,
        user: User | None,
        provider: str = DEFAULT_PROVIDER,
        requested: str | None = None,
    ) -> str:
        return (requested or "").strip() or user_service.provider_model(user, provider)

    def missing_message(self, provider: str) -> str:
        meta = self.meta(provider)
        if meta.local:
            return (
                "The local Qwen endpoint is not configured. Set the address in the user card "
                "(or QWEN_LOCAL_BASE_URL on the server) and point it at Ollama/vLLM "
                "(Ollama: http://host.docker.internal:11434/v1)."
            )
        if meta.name == "qwen":
            return (
                "No Qwen API key is assigned to this account. "
                "Ask an administrator to add one, or set QWEN_API_KEY in .env."
            )
        return (
            "No DeepSeek API key is assigned to this account. "
            "Ask an administrator to add one, or set DEEPSEEK_API_KEY in .env."
        )

    def _build(
        self, provider: str, user: User | None, model: str | None = None
    ) -> LLMProvider:
        meta = self.meta(provider)
        api_key = self.api_key_for(user, provider)
        if not api_key and meta.local:
            # Ollama and friends accept any non-empty bearer token.
            api_key = _LOCAL_PLACEHOLDER_KEY
        if not api_key:
            raise NoProviderAvailable(
                self.missing_message(provider), details={"provider": meta.name}
            )
        base_url = self.base_url_for(provider, user)
        chosen = self.model_for(user, provider, model)
        cache_key = (meta.name, fingerprint(api_key), chosen, base_url)
        cached = self._chat.get(cache_key)
        if cached is not None:
            return cached
        client = CompatibleProvider(
            meta,
            api_key=api_key,
            base_url=base_url,
            default_model=chosen,
            capabilities=ProviderCapabilities(
                supports_tools=meta.supports_tools,
                supports_vision=meta.supports_vision,
                supports_embeddings=meta.supports_embeddings,
            ),
            dynamic_models=meta.name != "deepseek",
        )
        self._chat[cache_key] = client
        return client

    async def resolve(
        self,
        db: AsyncSession,
        user: User | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> ResolvedProvider:
        candidates = await self.candidates(db, user, model, provider)
        return candidates[0]

    async def candidates(
        self,
        db: AsyncSession,
        user: User | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> list[ResolvedProvider]:
        """Providers that can serve this request, in fallback order.

        The explicitly requested provider wins, then the saved preference, then
        the static fallback order. Providers without credentials are skipped.
        """
        prefs = await self.preferences(db)
        requested = (provider or "").strip().lower() or None
        preferred = str(prefs.get("provider") or DEFAULT_PROVIDER).strip().lower()
        order: list[str] = []
        for name in (requested, preferred, *PROVIDER_NAMES):
            if not name or name in order or name not in PROVIDERS:
                continue
            order.append(name)

        selected_model = (model or "").strip()
        model_owner = requested or preferred
        resolved: list[ResolvedProvider] = []
        for name in order:
            if not self.is_available(user, name):
                continue
            try:
                client = self._build(
                    name, user, selected_model if name == model_owner else None
                )
            except NoProviderAvailable:
                continue
            except Exception as exc:  # noqa: BLE001 - one provider must not break chat
                logger.warning("%s client initialisation failed: %s", name, exc)
                continue
            resolved.append(
                ResolvedProvider(
                    provider=client,
                    provider_name=name,
                    model=client.default_model,
                    requested=requested,
                )
            )
        if resolved:
            return resolved
        raise NoProviderAvailable(
            "No chat provider is available. Ask an administrator to assign a DeepSeek "
            "or Qwen API key, or set QWEN_LOCAL_BASE_URL to use a local Qwen model.",
            details={
                "providers": list(PROVIDER_NAMES),
                "username": getattr(user, "username", None),
            },
        )

    async def get_provider(
        self,
        db: AsyncSession,
        user: User | None = None,
        provider: str | None = None,
    ) -> LLMProvider | None:
        name = (provider or DEFAULT_PROVIDER).strip().lower()
        if name not in PROVIDERS or not self.is_available(user, name):
            return None
        try:
            return self._build(name, user)
        except Exception as exc:  # noqa: BLE001 - callers treat None as unavailable
            logger.warning("%s client initialisation failed: %s", name, exc)
            return None

    async def is_configured(
        self,
        db: AsyncSession,
        user: User | None = None,
        provider: str | None = None,
    ) -> bool:
        name = (provider or DEFAULT_PROVIDER).strip().lower()
        if name not in PROVIDERS:
            return False
        return self.is_available(user, name)

    # -------------------------------------------------------- embeddings
    async def resolve_embeddings(
        self,
        db: AsyncSession | None = None,
        preferred: str | None = None,
        model: str | None = None,
    ) -> ResolvedEmbedding:
        """Vectors are always produced locally; there is no embeddings API."""
        if self._local is None:
            self._local = LocalEmbeddingProvider(settings.local_embedding_model)
        chosen = (model or "").strip() or settings.local_embedding_model
        return ResolvedEmbedding(self._local, "local", chosen)

    # --------------------------------------------------------- preferences
    async def preferences(self, db: AsyncSession) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "provider": DEFAULT_PROVIDER,
            "model": self.model_for(None, DEFAULT_PROVIDER),
            "embedding_provider": "local",
            "embedding_model": settings.local_embedding_model,
        }
        row = await db.get(AppSetting, PREFERENCES_KEY)
        if row is not None and isinstance(row.value, dict):
            for key, value in row.value.items():
                if key in defaults and isinstance(value, str) and value.strip():
                    defaults[key] = value.strip()
        if defaults["provider"] not in PROVIDERS:
            defaults["provider"] = DEFAULT_PROVIDER
        defaults["embedding_provider"] = "local"
        return defaults

    async def save_preferences(self, db: AsyncSession, values: dict[str, Any]) -> dict[str, Any]:
        current = await self.preferences(db)
        allowed = {"provider", "model"}
        updates = {
            key: str(value).strip()
            for key, value in values.items()
            if key in allowed and isinstance(value, str) and value.strip()
        }
        provider = updates.get("provider")
        if provider is not None:
            provider = provider.lower()
            if provider not in PROVIDERS:
                raise ValueError(f"Unknown provider: {provider}")
            updates["provider"] = provider
            if "model" not in updates:
                updates["model"] = self.model_for(None, provider)
        current.update(updates)
        row = await db.get(AppSetting, PREFERENCES_KEY)
        if row is None:
            row = AppSetting(key=PREFERENCES_KEY, value=current)
            db.add(row)
        else:
            row.value = dict(current)
        await db.flush()
        self.invalidate()
        return current

    # -------------------------------------------------------------- views
    def _view(
        self,
        meta: ProviderMeta,
        *,
        configured: bool,
        source: str,
        model: str,
        key: str = "",
        base_url: str | None = None,
    ) -> ProviderView:
        return ProviderView(
            name=meta.name,
            label=meta.label,
            configured=configured,
            enabled=True,
            source=source,
            models=list(meta.models),
            default_model=model,
            base_url=base_url,
            supports_tools=meta.supports_tools,
            supports_vision=meta.supports_vision,
            supports_embeddings=meta.supports_embeddings,
            local=meta.local,
            requires_api_key=meta.requires_api_key,
            api_key_masked=mask_secret(key),
        )

    async def provider_view(
        self,
        db: AsyncSession,
        user: User | None = None,
        provider: str | None = None,
    ) -> ProviderView:
        name = (provider or DEFAULT_PROVIDER).strip().lower()
        meta = self.meta(name)
        key = self.api_key_for(user, name)
        configured = self.is_available(user, name)
        if user_service.has_personal_key(user, name):
            source = "user"
        elif configured and meta.local:
            source = "local"
        elif configured:
            source = "environment"
        else:
            source = "none"
        return self._view(
            meta,
            configured=configured,
            source=source,
            model=self.model_for(user, name),
            key=key,
            base_url=self.base_url_for(name, user),
        )

    async def list_providers(
        self, db: AsyncSession, user: User | None = None
    ) -> list[ProviderView]:
        return [await self.provider_view(db, user, name) for name in PROVIDER_NAMES]

    async def embedding_options(self) -> list[dict[str, Any]]:
        available = local_embeddings_available()
        return [
            {
                "name": name,
                "label": {"local": "Local sentence-transformers"}.get(name, name.title()),
                "configured": available,
                "models": list(EMBEDDING_MODELS.get(name, ())),
            }
            for name in EMBEDDING_PROVIDERS
        ]

    async def health(
        self,
        db: AsyncSession,
        user: User | None = None,
        provider: str | None = None,
    ) -> ProviderHealth:
        name = (provider or DEFAULT_PROVIDER).strip().lower()
        if name not in PROVIDERS:
            return ProviderHealth(ok=False, message=f"Unknown provider: {name}")
        if not self.is_available(user, name):
            return ProviderHealth(ok=False, message=self.missing_message(name))
        try:
            client = self._build(name, user)
        except NoProviderAvailable as exc:
            return ProviderHealth(ok=False, message=exc.message)
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(ok=False, message=f"{type(exc).__name__}: {exc}")
        return await client.health()

    async def test(
        self,
        db: AsyncSession,
        user: User | None = None,
        provider: str | None = None,
    ) -> ProviderHealth:
        return await self.health(db, user, provider)


registry = ProviderRegistry()
