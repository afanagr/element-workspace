"""Anthropic provider: messages/tool-use mapping onto the unified stream schema."""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Sequence

import anthropic
from anthropic import AsyncAnthropic

from app.config import settings
from app.llm.base import LLMProvider, ProviderCapabilities, ProviderHealth
from app.llm.schemas import (
    ChatMessage,
    ProviderError,
    StreamEnd,
    StreamEvent,
    TextDelta,
    TokenUsage,
    ToolCall,
    ToolCallEvent,
    ToolSpec,
)

ANTHROPIC_MODELS = [
    "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
]


def _image_block(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header[5:].split(";")[0] or "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    label = "Anthropic"
    default_model = "claude-sonnet-4-20250514"
    models = ANTHROPIC_MODELS
    capabilities = ProviderCapabilities(
        supports_tools=True, supports_vision=True, supports_embeddings=False
    )

    def __init__(self, *, api_key: str, default_model: str | None = None) -> None:
        if not api_key:
            raise ProviderError("Anthropic: API key is not configured", provider=self.name, code="missing_key")
        self.default_model = default_model or self.default_model
        self._client = AsyncAnthropic(
            api_key=api_key,
            timeout=settings.llm_request_timeout,
            max_retries=0,
        )

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _convert(messages: Sequence[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        for message in messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
                continue

            if message.role == "tool":
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content or "",
                }
                role, content = "user", [block]
            elif message.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                if not blocks:
                    blocks.append({"type": "text", "text": "(empty)"})
                role, content = "assistant", blocks
            else:
                blocks = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for image in message.images:
                    blocks.append(_image_block(image))
                role, content = "user", blocks or [{"type": "text", "text": ""}]

            if converted and converted[-1]["role"] == role:
                converted[-1]["content"].extend(content)
            else:
                converted.append({"role": role, "content": content})

        return "\n\n".join(system_parts), converted

    @staticmethod
    def _convert_tools(tools: Sequence[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in tools
        ]

    @staticmethod
    def _map_error(exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, anthropic.RateLimitError):
            return ProviderError(
                "Rate limit reached (HTTP 429). Try again or switch provider.",
                provider="anthropic",
                status_code=429,
                retryable=True,
                code="rate_limit",
            )
        if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError, asyncio.TimeoutError)):
            return ProviderError(
                f"Cannot reach anthropic: {exc}", provider="anthropic", retryable=True, code="connection"
            )
        if isinstance(exc, anthropic.APIStatusError):
            status = exc.status_code
            detail = ""
            try:
                body = exc.response.json()
                if isinstance(body, dict):
                    error = body.get("error")
                    detail = str(error.get("message")) if isinstance(error, dict) else str(body.get("message") or "")
            except Exception:  # pragma: no cover
                detail = ""
            return ProviderError(
                f"HTTP {status}: {detail or getattr(exc, 'message', str(exc))}",
                provider="anthropic",
                status_code=status,
                retryable=status == 429 or status >= 500,
                code="rate_limit" if status == 429 else ("auth" if status in (401, 403) else None),
            )
        return ProviderError(f"anthropic: {type(exc).__name__}: {exc}", provider="anthropic")

    # -------------------------------------------------------------- chat
    async def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        resolved = self.resolve_model(model)
        system, converted = self._convert(messages)
        kwargs: dict[str, Any] = {
            "model": resolved,
            "max_tokens": max_tokens or 4096,
            "messages": converted,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        tool_payload = self._convert_tools(tools)
        if tool_payload:
            kwargs["tools"] = tool_payload

        attempts = max(1, settings.llm_max_retries + 1)
        for attempt in range(attempts):
            emitted = False
            text_parts: list[str] = []
            tool_blocks: dict[int, dict[str, Any]] = {}
            final_message: Any = None
            try:
                async with self._client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        event_type = getattr(event, "type", "")
                        if event_type == "content_block_start":
                            block = getattr(event, "content_block", None)
                            if block is not None and getattr(block, "type", "") == "tool_use":
                                tool_blocks[event.index] = {
                                    "id": getattr(block, "id", ""),
                                    "name": getattr(block, "name", ""),
                                    "json": "",
                                }
                        elif event_type == "content_block_delta":
                            delta = event.delta
                            delta_type = getattr(delta, "type", "")
                            if delta_type == "text_delta":
                                emitted = True
                                text_parts.append(delta.text)
                                yield TextDelta(text=delta.text)
                            elif delta_type == "input_json_delta":
                                slot = tool_blocks.setdefault(
                                    event.index, {"id": "", "name": "", "json": ""}
                                )
                                slot["json"] += getattr(delta, "partial_json", "") or ""
                    final_message = await stream.get_final_message()
            except Exception as exc:  # noqa: BLE001
                error = self._map_error(exc)
                if not emitted and attempt + 1 < attempts and error.retryable:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                raise error from exc

            usage = TokenUsage()
            final_usage = getattr(final_message, "usage", None)
            if final_usage is not None:
                prompt_tokens = int(getattr(final_usage, "input_tokens", 0) or 0)
                completion_tokens = int(getattr(final_usage, "output_tokens", 0) or 0)
                usage = TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                )
            tool_calls = [
                ToolCall(
                    id=slot["id"] or f"tool_{index}",
                    name=slot["name"],
                    arguments=parse_tool_arguments(slot["json"]),
                    raw_arguments=slot["json"],
                )
                for index, slot in sorted(tool_blocks.items(), key=lambda item: item[0])
                if slot["name"]
            ]
            for call in tool_calls:
                yield ToolCallEvent(call=call)
            yield StreamEnd(
                message=ChatMessage(
                    role="assistant",
                    content="".join(text_parts) or None,
                    tool_calls=tool_calls,
                ),
                usage=usage,
                finish_reason=str(getattr(final_message, "stop_reason", None) or "stop"),
            )
            return
        raise ProviderError("anthropic: retries exhausted", provider=self.name, retryable=True)

    # ------------------------------------------------------------ health
    async def list_models(self) -> list[str]:
        try:
            page = await self._client.models.list(limit=50)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        ids = [model.id for model in getattr(page, "data", []) if getattr(model, "id", None)]
        return ids or list(self.models)

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            try:
                ids = await self.list_models()
            except ProviderError:
                # Some keys/regions do not expose /v1/models: fall back to a 1-token probe.
                await self._client.messages.create(
                    model=self.default_model,
                    max_tokens=1,
                    messages=[{"role": "user", "content": "ping"}],
                )
                ids = [self.default_model]
            return ProviderHealth(
                ok=True,
                message=f"Connected · {len(ids)} model(s) available",
                latency_ms=(time.perf_counter() - started) * 1000,
                models=ids[:50],
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(
                ok=False,
                message=self._map_error(exc).message,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

    async def aclose(self) -> None:
        await self._client.close()


