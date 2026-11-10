"""Local sentence-transformers embeddings (offline fallback for providers without them)."""

from __future__ import annotations

import asyncio
import importlib.util
import time
from typing import Any, AsyncIterator, Sequence

from app.config import settings
from app.llm.base import LLMProvider, ProviderCapabilities, ProviderHealth
from app.llm.schemas import ChatMessage, ProviderError, StreamEvent, ToolSpec
from app.logging_config import get_logger

logger = get_logger(__name__)


def local_embeddings_available() -> bool:
    """True when sentence-transformers is installed in this image."""
    try:
        return importlib.util.find_spec("sentence_transformers") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken site-packages
        return False


class LocalEmbeddingProvider(LLMProvider):
    """CPU-only embedding backend.

    Used when ``EMBEDDING_PROVIDER=local`` (the automatic choice when the chat
    provider has no native embedding endpoint). Model files are downloaded on
    first use into ``HF_HOME`` which is a volume, so restarts stay offline-fast.
    """

    name = "local"
    label = "Local (sentence-transformers)"
    default_model = settings.local_embedding_model
    models = [settings.local_embedding_model]
    capabilities = ProviderCapabilities(
        supports_tools=False, supports_vision=False, supports_embeddings=True
    )

    def __init__(self, model_name: str | None = None) -> None:
        self.default_model = model_name or settings.local_embedding_model
        self.models = [self.default_model]
        self._model: Any = None
        self._lock = asyncio.Lock()

    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise ProviderError(
            "The local provider only provides embeddings, not chat",
            provider=self.name,
            code="chat_unsupported",
        )

    async def _load(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is not None:
                return self._model
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - depends on build arg
                raise ProviderError(
                    "sentence-transformers is not installed in this image. "
                    "Rebuild with INSTALL_LOCAL_EMBEDDINGS=1 or choose another EMBEDDING_PROVIDER.",
                    provider=self.name,
                    code="dependency_missing",
                ) from exc
            logger.info("Loading local embedding model %s", self.default_model)
            started = time.perf_counter()
            self._model = await asyncio.to_thread(SentenceTransformer, self.default_model)
            logger.info("Local embedding model ready in %.1fs", time.perf_counter() - started)
        return self._model

    async def embed(self, texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        model_obj = await self._load()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), settings.embedding_batch_size):
            batch = list(texts[start : start + settings.embedding_batch_size])
            try:
                encoded = await asyncio.to_thread(
                    model_obj.encode,
                    batch,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                )
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"local embeddings failed: {exc}", provider=self.name) from exc
            vectors.extend([[float(value) for value in row] for row in encoded])
        return vectors

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            await self._load()
            return ProviderHealth(
                ok=True,
                message=f"Loaded · {self.default_model}",
                latency_ms=(time.perf_counter() - started) * 1000,
                models=list(self.models),
            )
        except ProviderError as exc:
            return ProviderHealth(
                ok=False,
                message=exc.message,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
