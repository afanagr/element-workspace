"""DeepSeek client: OpenAI-compatible chat completions with tool calling."""

from __future__ import annotations

from app.llm.base import ProviderCapabilities
from app.llm.catalog import ProviderMeta
from app.llm.openai_provider import OpenAIProvider


class CompatibleProvider(OpenAIProvider):
    """DeepSeek speaks the OpenAI chat-completions dialect.

    It supports tool calling and ``stream_options.include_usage``, but has no
    vision and no embeddings endpoint.
    """

    def __init__(
        self,
        meta: ProviderMeta,
        *,
        api_key: str,
        base_url: str | None = None,
        default_model: str | None = None,
        capabilities: ProviderCapabilities | None = None,
        dynamic_models: bool = False,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url or meta.base_url or "",
            name=meta.name,
            label=meta.label,
            models=list(meta.models),
            default_model=default_model or meta.default_model,
            capabilities=capabilities
            or ProviderCapabilities(
                supports_tools=True, supports_vision=False, supports_embeddings=False
            ),
            supports_stream_options=True,
        )
        self._dynamic_models = dynamic_models

    async def list_models(self) -> list[str]:
        ids = await super().list_models()
        if self._dynamic_models and ids:
            self.models = sorted(set(ids))
            return self.models
        return ids

