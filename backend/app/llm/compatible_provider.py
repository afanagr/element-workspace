"""OpenAI-compatible clients: DeepSeek, Qwen (DashScope) and local Qwen servers.

All three speak the OpenAI chat-completions dialect (tool calling and
``stream_options.include_usage`` included), so they share one client class.
"""

from __future__ import annotations

import asyncio
import time

from app.config import settings
from app.llm.base import ProviderCapabilities, ProviderHealth
from app.llm.catalog import ProviderMeta
from app.llm.openai_provider import OpenAIProvider
from app.llm.schemas import ProviderError

HEALTH_TIMEOUT_SECONDS = 20.0


class CompatibleProvider(OpenAIProvider):
    """OpenAI-compatible chat backend with an optional dynamic model list."""

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
            known = list(dict.fromkeys([*ids, *self.models]))
            self.models = sorted(known)
            return self.models
        return ids

    async def health(self) -> ProviderHealth:
        """Model listing is optional: some gateways do not expose ``/models``."""
        started = time.perf_counter()
        try:
            ids = await asyncio.wait_for(
                self.list_models(), timeout=min(settings.llm_request_timeout, HEALTH_TIMEOUT_SECONDS)
            )
            return ProviderHealth(
                ok=True,
                message=f"Connected · {len(ids)} model(s) available",
                latency_ms=(time.perf_counter() - started) * 1000,
                models=sorted(ids)[:50],
            )
        except ProviderError as exc:
            if exc.status_code in (404, 405):
                return ProviderHealth(
                    ok=True,
                    message="Connected · this endpoint does not expose a model list",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    models=list(self.models),
                )
            return ProviderHealth(
                ok=False,
                message=exc.message,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return ProviderHealth(
                ok=False,
                message="Timed out while checking the endpoint",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

