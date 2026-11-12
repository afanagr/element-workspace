"""DeepSeek provider registry: construction, per-user keys and local embeddings.

Every account carries its own DeepSeek API key (issued by an administrator). The
deployment-wide ``DEEPSEEK_API_KEY`` is only a fallback for accounts without a
personal key. There is no provider fallback any more: DeepSeek is the single
chat backend, so the registry is deliberately small.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.base import LLMProvider, ProviderCapabilities, ProviderHealth
from app.llm.catalog import EMBEDDING_MODELS, EMBEDDING_PROVIDERS, PROVIDERS, ProviderMeta
from app.llm.compatible_provider import CompatibleProvider
from app.llm.local_embeddings import LocalEmbeddingProvider, local_embeddings_available
from app.llm.schemas import NoProviderAvailable
from app.logging_config import get_logger
from app.models import AppSetting, User
from app.security import fingerprint, mask_secret
from app.services import users as user_service

logger = get_logger(__name__)

PROVIDER_NAME = "deepseek"
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


class ProviderRegistry:
    """Builds DeepSeek clients from the key assigned to a user."""

    def __init__(self) -> None:
        self._chat: dict[tuple[str, str], LLMProvider] = {}
        self._local: LocalEmbeddingProvider | None = None

    # ---------------------------------------------------------- lifecycle
    def invalidate(self) -> None:
        self._chat.clear()
        self._local = None

    def _meta(self) -> ProviderMeta:
        return PROVIDERS[PROVIDER_NAME]

    def _build(self, api_key: str, model: str) -> LLMProvider:
        meta = self._meta()
        cache_key = (fingerprint(api_key), model)
        cached = self._chat.get(cache_key)
        if cached is not None:
            return cached
        provider = CompatibleProvider(
            meta,
            api_key=api_key,
            base_url=meta.base_url,
            default_model=model,
            capabilities=ProviderCapabilities(
                supports_tools=True, supports_vision=False, supports_embeddings=False
            ),
        )
        self._chat[cache_key] = provider
        return provider

    # --------------------------------------------------------------- chat
    def api_key_for(self, user: User | None) -> str:
        return user_service.deepseek_key(user)

    def model_for(self, user: User | None, requested: str | None = None) -> str:
        return (requested or "").strip() or user_service.deepseek_model(user)

    async def resolve(
        self,
        db: AsyncSession,
        user: User | None = None,
        model: str | None = None,
    ) -> ResolvedProvider:
        candidates = await self.candidates(db, user, model)
        return candidates[0]

    async def candidates(
        self,
        db: AsyncSession,
        user: User | None = None,
        model: str | None = None,
    ) -> list[ResolvedProvider]:
        """DeepSeek is the only backend, so the list holds at most one entry."""
        api_key = self.api_key_for(user)
        if not api_key:
            raise NoProviderAvailable(
                "No DeepSeek API key is assigned to this account. "
                "Ask an administrator to add one, or set DEEPSEEK_API_KEY in .env.",
                details={"provider": PROVIDER_NAME, "username": getattr(user, "username", None)},
            )
        chosen = self.model_for(user, model)
        try:
            provider = self._build(api_key, chosen)
        except Exception as exc:  # noqa: BLE001 - never let one provider break the app
            logger.warning("DeepSeek client initialisation failed: %s", exc)
            raise NoProviderAvailable(
                "The DeepSeek client could not be initialised.", details={"error": str(exc)}
            ) from exc
        return [ResolvedProvider(provider=provider, provider_name=PROVIDER_NAME, model=chosen)]

    async def get_provider(self, db: AsyncSession, user: User | None = None) -> LLMProvider | None:
        api_key = self.api_key_for(user)
        if not api_key:
            return None
        return self._build(api_key, self.model_for(user))

    async def is_configured(self, db: AsyncSession, user: User | None = None) -> bool:
        return bool(self.api_key_for(user))

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
            "provider": PROVIDER_NAME,
            "model": settings.llm_model,
            "embedding_provider": "local",
            "embedding_model": settings.local_embedding_model,
        }
        row = await db.get(AppSetting, PREFERENCES_KEY)
        if row is not None and isinstance(row.value, dict):
            for key, value in row.value.items():
                if value not in (None, ""):
                    defaults[key] = value
        defaults["provider"] = PROVIDER_NAME
        defaults["embedding_provider"] = "local"
        return defaults

    async def save_preferences(self, db: AsyncSession, values: dict[str, Any]) -> dict[str, Any]:
        current = await self.preferences(db)
        allowed = {"model"}
        current.update({key: value for key, value in values.items() if key in allowed and value})
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
    def _view(self, *, configured: bool, source: str, model: str, key: str = "") -> ProviderView:
        meta = self._meta()
        return ProviderView(
            name=meta.name,
            label=meta.label,
            configured=configured,
            enabled=True,
            source=source,
            models=list(meta.models),
            default_model=model,
            base_url=meta.base_url,
            supports_tools=meta.supports_tools,
            supports_vision=meta.supports_vision,
            supports_embeddings=meta.supports_embeddings,
            local=False,
            api_key_masked=mask_secret(key),
        )

    async def provider_view(self, db: AsyncSession, user: User | None = None) -> ProviderView:
        own_key = user_service.deepseek_key(user) if user is not None else ""
        has_personal = bool(user is not None and user.deepseek_api_key_enc)
        source = "user" if has_personal else ("environment" if settings.deepseek_api_key else "none")
        return self._view(
            configured=bool(own_key),
            source=source if own_key else "none",
            model=self.model_for(user),
            key=own_key,
        )

    async def list_providers(
        self, db: AsyncSession, user: User | None = None
    ) -> list[ProviderView]:
        return [await self.provider_view(db, user)]

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

    async def health(self, db: AsyncSession, user: User | None = None) -> ProviderHealth:
        provider = await self.get_provider(db, user)
        if provider is None:
            return ProviderHealth(ok=False, message="No DeepSeek API key assigned")
        return await provider.health()

    async def test(self, db: AsyncSession, user: User | None = None) -> ProviderHealth:
        return await self.health(db, user)


registry = ProviderRegistry()

