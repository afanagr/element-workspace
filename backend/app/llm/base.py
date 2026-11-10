"""The single LLMProvider interface implemented by every backend."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import AsyncIterator, Sequence

from app.llm.schemas import ChatMessage, ProviderError, StreamEvent, ToolSpec


@dataclass(slots=True)
class ProviderCapabilities:
    supports_tools: bool = True
    supports_vision: bool = False
    supports_embeddings: bool = False


@dataclass(slots=True)
class ProviderHealth:
    ok: bool
    message: str
    latency_ms: float = 0.0
    models: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "message": self.message,
            "latency_ms": round(self.latency_ms, 1),
            "models": self.models,
        }


class LLMProvider(abc.ABC):
    """Unified provider contract: chat streaming, embeddings, capabilities."""

    name: str = "base"
    label: str = "Base"
    default_model: str = ""
    models: list[str] = []
    capabilities: ProviderCapabilities = ProviderCapabilities()

    # ------------------------------------------------------------- chat
    @abc.abstractmethod
    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream text deltas, complete tool calls and a final StreamEnd."""
        raise NotImplementedError

    # -------------------------------------------------------- embeddings
    async def embed(self, texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
        raise ProviderError(
            f"{self.label} does not provide embeddings",
            provider=self.name,
            code="embeddings_unsupported",
        )

    # ------------------------------------------------------------ health
    async def health(self) -> ProviderHealth:
        raise NotImplementedError

    async def list_models(self) -> list[str]:
        return list(self.models)

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------- utils
    @property
    def supports_tools(self) -> bool:
        return self.capabilities.supports_tools

    @property
    def supports_vision(self) -> bool:
        return self.capabilities.supports_vision

    @property
    def supports_embeddings(self) -> bool:
        return self.capabilities.supports_embeddings

    def resolve_model(self, model: str | None) -> str:
        return model or self.default_model

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<{type(self).__name__} name={self.name} model={self.default_model}>"
