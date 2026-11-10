"""Embedding service: batching + retries around a resolved embedding provider."""

from __future__ import annotations

import asyncio
from typing import Sequence

from app.config import settings
from app.llm.base import LLMProvider
from app.llm.schemas import ProviderError
from app.logging_config import get_logger

logger = get_logger(__name__)


class EmbeddingService:
    def __init__(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self._dimension: int | None = None

    @property
    def dimension(self) -> int | None:
        return self._dimension

    async def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        attempts = max(1, settings.llm_max_retries + 1)
        last_error: ProviderError | None = None
        for attempt in range(attempts):
            try:
                vectors = await self.provider.embed(batch, model=self.model)
                if len(vectors) != len(batch):
                    raise ProviderError(
                        f"Embedding count mismatch ({len(vectors)} != {len(batch)})",
                        provider=self.provider.name,
                    )
                return vectors
            except ProviderError as exc:
                last_error = exc
                if attempt + 1 < attempts and exc.retryable:
                    delay = 1.0 * (attempt + 1)
                    logger.warning(
                        "Embedding request failed (%s); retrying in %.1fs", exc.message, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise ProviderError("Embedding request failed", provider=self.provider.name)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        cleaned = [text if text.strip() else "(empty)" for text in texts]
        vectors: list[list[float]] = []
        size = max(1, settings.embedding_batch_size)
        for start in range(0, len(cleaned), size):
            batch = cleaned[start : start + size]
            vectors.extend(await self._embed_batch(batch))
        if vectors:
            self._dimension = len(vectors[0])
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed_batch([text])
        if not vectors:
            raise ProviderError("Empty query embedding", provider=self.provider.name)
        self._dimension = len(vectors[0])
        return vectors[0]
