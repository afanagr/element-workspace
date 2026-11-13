"""Generic OpenAI-compatible chat client.

Every model connection (DeepSeek, DashScope/Qwen, Ollama, vLLM, LM Studio, an
internal gateway, ...) is just a base URL + API key + model name, so one client
class serves all of them.
"""

from __future__ import annotations

import asyncio
import time
from typing import Sequence

from app.config import settings
from app.llm.base import ProviderCapabilities, ProviderHealth
from app.llm.openai_provider import OpenAIProvider
from app.llm.schemas import ProviderError

HEALTH_TIMEOUT_SECONDS = 20.0


class CompatibleProvider(OpenAIProvider):
    """OpenAI-compatible chat backend with an optional dynamic model list."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        base_url: str,
        default_model: str,
        label: str | None = None,
        models: Sequence[str] | None = None,
        supports_tools: bool = True,
        dynamic_models: bool = True,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            name=name,
            label=label or name,
            models=list(models) if models else [default_model],
            default_model=default_model,
            capabilities=ProviderCapabilities(
                supports_tools=supports_tools,
                supports_vision=False,
                supports_embeddings=False,
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
