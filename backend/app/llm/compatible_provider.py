"""OpenAI-compatible providers: DeepSeek, OpenRouter, Ollama, Groq, Together, xAI."""

from __future__ import annotations

from app.llm.base import ProviderCapabilities
from app.llm.catalog import PROVIDERS, ProviderMeta
from app.llm.openai_provider import OpenAIProvider


class CompatibleProvider(OpenAIProvider):
    """All providers speaking the OpenAI chat-completions dialect.

    Differences that matter in practice:
      * ``stream_options={"include_usage": true}`` is not implemented by every
        backend (Ollama, some OpenRouter routes) - it is opt-in per provider.
      * Ollama ignores the API key but the SDK requires a non-empty value.
      * Ollama's model list is dynamic, so ``list_models`` refreshes ``models``.
    """

    def __init__(
        self,
        meta: ProviderMeta,
        *,
        api_key: str,
        base_url: str | None = None,
        default_model: str | None = None,
        dynamic_models: bool = False,
    ) -> None:
        resolved_base = base_url or meta.base_url or ""
        capabilities = ProviderCapabilities(
            supports_tools=meta.supports_tools,
            supports_vision=meta.supports_vision,
            supports_embeddings=meta.supports_embeddings,
        )
        extra_headers: dict[str, str] = {}
        if meta.name == "openrouter":
            extra_headers = {
                "HTTP-Referer": "http://localhost:3000",
                "X-Title": "Element Workspace",
            }
        # Only backends known to implement `stream_options.include_usage`.
        supports_stream_options = meta.name in {"deepseek", "openrouter"}
        super().__init__(
            api_key=api_key or ("ollama" if meta.local else ""),
            base_url=resolved_base,
            name=meta.name,
            label=meta.label,
            models=list(meta.models),
            default_model=default_model or meta.default_model,
            capabilities=capabilities,
            extra_headers=extra_headers,
            supports_stream_options=supports_stream_options,
        )
        self._dynamic_models = dynamic_models or meta.local

    async def list_models(self) -> list[str]:
        ids = await super().list_models()
        if self._dynamic_models and ids:
            # Ollama reports every locally pulled model - prefer that live list.
            self.models = sorted(set(ids))
            return self.models
        return ids


def build_compatible_provider(
    name: str,
    *,
    api_key: str,
    base_url: str | None = None,
    default_model: str | None = None,
) -> CompatibleProvider:
    meta = PROVIDERS.get(name)
    if meta is None:
        raise ValueError(f"Unknown OpenAI-compatible provider: {name}")
    return CompatibleProvider(meta, api_key=api_key, base_url=base_url, default_model=default_model)
