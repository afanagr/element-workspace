"""Runtime chat registry.

There is no built-in provider catalogue: every account owns a list of
OpenAI-compatible connections (name, base URL, encrypted API key, model). The
registry turns those rows into clients, resolves the connection requested by
the chat composer, falls back to the other connections and caches clients.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.base import LLMProvider, ProviderHealth
from app.llm.catalog import EMBEDDING_MODELS, EMBEDDING_PROVIDERS
from app.llm.compatible_provider import CompatibleProvider
from app.llm.local_embeddings import LocalEmbeddingProvider, local_embeddings_available
from app.llm.schemas import NoProviderAvailable
from app.logging_config import get_logger
from app.models import ModelConnection, User
from app.security import decrypt_secret, fingerprint, mask_secret

logger = get_logger(__name__)

NO_CONNECTIONS_MESSAGE = (
    "No models are connected to this account. Ask an administrator to add a "
    "connection (name, address, key, model) in /admin → Users."
)
_PLACEHOLDER_KEY = "no-api-key"


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
    requires_api_key: bool = False
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
    """Builds chat clients from the model connections of one account."""

    def __init__(self) -> None:
        self._chat: dict[tuple[str, str, str, str], LLMProvider] = {}
        self._local: LocalEmbeddingProvider | None = None

    def invalidate(self) -> None:
        self._chat.clear()
        self._local = None

    # ---------------------------------------------------------- connections
    async def list_connections(
        self, db: AsyncSession, user: User | None
    ) -> list[ModelConnection]:
        if user is None:
            return []
        result = await db.execute(
            select(ModelConnection)
            .where(ModelConnection.user_id == user.id)
            .order_by(ModelConnection.name)
        )
        return list(result.scalars().all())

    async def _find_connection(
        self, db: AsyncSession, user: User | None, provider: str | None
    ) -> ModelConnection | None:
        connections = await self.list_connections(db, user)
        if not connections:
            return None
        wanted = (provider or "").strip().lower()
        if not wanted:
            return connections[0]
        for connection in connections:
            if connection.name.lower() == wanted:
                return connection
        return None

    def _build(self, connection: ModelConnection, model: str | None = None) -> LLMProvider:
        raw_key = decrypt_secret(connection.api_key_enc)
        api_key = raw_key or _PLACEHOLDER_KEY
        chosen = (model or "").strip() or connection.model
        cache_key = (str(connection.id), fingerprint(api_key), connection.base_url, chosen)
        cached = self._chat.get(cache_key)
        if cached is not None:
            return cached
        client = CompatibleProvider(
            name=connection.name,
            label=connection.name,
            api_key=api_key,
            base_url=connection.base_url,
            default_model=chosen,
            models=[connection.model],
            supports_tools=connection.supports_tools,
            dynamic_models=True,
        )
        self._chat[cache_key] = client
        return client

    # --------------------------------------------------------------- chat
    async def candidates(
        self,
        db: AsyncSession,
        user: User | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> list[ResolvedProvider]:
        """Connections that can serve the request, requested one first."""
        connections = await self.list_connections(db, user)
        if not connections:
            raise NoProviderAvailable(NO_CONNECTIONS_MESSAGE)

        requested = (provider or "").strip()
        requested_key = requested.lower()
        selected_model = (model or "").strip()
        ordered = sorted(
            connections,
            key=lambda item: 0 if item.name.lower() == requested_key else 1,
        )
        resolved: list[ResolvedProvider] = []
        for connection in ordered:
            use_model = (
                selected_model
                if requested_key and connection.name.lower() == requested_key
                else None
            )
            try:
                client = self._build(connection, use_model)
            except Exception as exc:  # noqa: BLE001 - one model must not break chat
                logger.warning("Model connection %s failed: %s", connection.name, exc)
                continue
            resolved.append(
                ResolvedProvider(
                    provider=client,
                    provider_name=connection.name,
                    model=client.default_model,
                    requested=requested or None,
                )
            )
        if resolved:
            return resolved
        raise NoProviderAvailable(NO_CONNECTIONS_MESSAGE)

    async def resolve(
        self,
        db: AsyncSession,
        user: User | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> ResolvedProvider:
        candidates = await self.candidates(db, user, model, provider)
        return candidates[0]

    async def get_provider(
        self,
        db: AsyncSession,
        user: User | None = None,
        provider: str | None = None,
    ) -> LLMProvider | None:
        connection = await self._find_connection(db, user, provider)
        if connection is None:
            return None
        try:
            return self._build(connection)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Model connection %s failed: %s", connection.name, exc)
            return None

    async def is_configured(
        self,
        db: AsyncSession,
        user: User | None = None,
        provider: str | None = None,
    ) -> bool:
        return await self._find_connection(db, user, provider) is not None

    # -------------------------------------------------------------- views
    def _view(self, connection: ModelConnection) -> ProviderView:
        key = decrypt_secret(connection.api_key_enc)
        return ProviderView(
            name=connection.name,
            label=connection.name,
            configured=True,
            enabled=True,
            source="user",
            models=[connection.model],
            default_model=connection.model,
            base_url=connection.base_url,
            supports_tools=connection.supports_tools,
            supports_vision=False,
            supports_embeddings=False,
            local=False,
            api_key_masked=mask_secret(key),
            requires_api_key=bool(connection.api_key_enc),
        )

    async def provider_view(
        self,
        db: AsyncSession,
        user: User | None = None,
        provider: str | None = None,
    ) -> ProviderView | None:
        connection = await self._find_connection(db, user, provider)
        return self._view(connection) if connection is not None else None

    async def list_providers(
        self, db: AsyncSession, user: User | None = None
    ) -> list[ProviderView]:
        connections = await self.list_connections(db, user)
        return [self._view(connection) for connection in connections]

    # -------------------------------------------------------- embeddings
    async def resolve_embeddings(
        self,
        db: AsyncSession | None = None,
        preferred: str | None = None,
        model: str | None = None,
    ) -> ResolvedEmbedding:
        """Vectors are always produced locally; chat endpoints are text-only."""
        if self._local is None:
            self._local = LocalEmbeddingProvider(settings.local_embedding_model)
        chosen = (model or "").strip() or settings.local_embedding_model
        return ResolvedEmbedding(self._local, "local", chosen)

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

    # -------------------------------------------------------------- health
    async def health(
        self,
        db: AsyncSession,
        user: User | None = None,
        provider: str | None = None,
    ) -> ProviderHealth:
        connection = await self._find_connection(db, user, provider)
        if connection is None:
            if provider:
                return ProviderHealth(ok=False, message=f"Model connection '{provider}' was not found")
            return ProviderHealth(ok=False, message=NO_CONNECTIONS_MESSAGE)
        try:
            client = self._build(connection)
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
