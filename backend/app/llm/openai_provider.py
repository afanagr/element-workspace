"""Native OpenAI provider (also the base class for OpenAI-compatible APIs)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Sequence

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

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
    is_retryable_status,
    parse_tool_arguments,
)
from app.logging_config import get_logger

logger = get_logger(__name__)


def map_openai_error(exc: Exception, provider: str) -> ProviderError:
    """Turn any openai-sdk exception into a ProviderError with UI-friendly flags."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, RateLimitError):
        return ProviderError(
            "Rate limit reached (HTTP 429). Try again or switch provider.",
            provider=provider,
            status_code=429,
            retryable=True,
            code="rate_limit",
        )
    if isinstance(exc, (APITimeoutError, APIConnectionError, asyncio.TimeoutError)):
        return ProviderError(
            f"Cannot reach {provider}: {exc}",
            provider=provider,
            retryable=True,
            code="connection",
        )
    if isinstance(exc, APIStatusError):
        detail = ""
        try:
            body = exc.response.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    detail = str(error.get("message") or "")
                detail = detail or str(body.get("message") or "")
        except Exception:  # pragma: no cover - best effort body decoding
            detail = ""
        status = exc.status_code
        return ProviderError(
            f"HTTP {status}: {detail or getattr(exc, 'message', str(exc))}",
            provider=provider,
            status_code=status,
            retryable=is_retryable_status(status),
            code="rate_limit" if status == 429 else ("auth" if status in (401, 403) else None),
        )
    return ProviderError(f"{provider}: {type(exc).__name__}: {exc}", provider=provider, retryable=False)


def build_user_content(message: ChatMessage) -> Any:
    if not message.images:
        return message.content or ""
    parts: list[dict[str, Any]] = []
    if message.content:
        parts.append({"type": "text", "text": message.content})
    for image in message.images:
        parts.append({"type": "image_url", "image_url": {"url": image}})
    return parts


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        name: str = "openai",
        label: str = "OpenAI",
        models: Sequence[str] | None = None,
        default_model: str = "gpt-4o",
        capabilities: ProviderCapabilities | None = None,
        extra_headers: dict[str, str] | None = None,
        supports_stream_options: bool = True,
    ) -> None:
        if not api_key:
            raise ProviderError(f"{label}: API key is not configured", provider=name, code="missing_key")
        self.name = name
        self.label = label
        self.default_model = default_model
        self.models = list(models or [default_model])
        self.capabilities = capabilities or ProviderCapabilities(
            supports_tools=True, supports_vision=True, supports_embeddings=False
        )
        self._supports_stream_options = supports_stream_options
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=settings.llm_request_timeout,
            max_retries=0,
            default_headers=extra_headers or None,
        )

    # ------------------------------------------------------------ helpers
    def _convert_messages(self, messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "tool":
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id or "",
                        "content": message.content or "",
                    }
                )
            elif message.role == "assistant":
                payload: dict[str, Any] = {"role": "assistant", "content": message.content or None}
                if message.tool_calls:
                    payload["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.raw_arguments
                                or json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in message.tool_calls
                    ]
                if payload["content"] is None and "tool_calls" not in payload:
                    payload["content"] = ""
                converted.append(payload)
            else:
                converted.append({"role": message.role, "content": build_user_content(message)})
        return converted

    @staticmethod
    def _convert_tools(tools: Sequence[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

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
        payload: dict[str, Any] = {
            "model": resolved,
            "messages": self._convert_messages(messages),
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        tool_payload = self._convert_tools(tools)
        if tool_payload:
            payload["tools"] = tool_payload
            payload["tool_choice"] = "auto"
        if self._supports_stream_options:
            payload["stream_options"] = {"include_usage": True}

        attempts = max(1, settings.llm_max_retries + 1)
        for attempt in range(attempts):
            emitted = False
            try:
                stream = await self._client.chat.completions.create(**payload)
            except Exception as exc:  # noqa: BLE001 - normalised below
                error = map_openai_error(exc, self.name)
                if attempt + 1 < attempts and error.retryable:
                    logger.warning(
                        "%s: request failed (%s), retrying %s/%s",
                        self.name,
                        error.message,
                        attempt + 1,
                        attempts - 1,
                    )
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                raise error from exc

            text_parts: list[str] = []
            slots: dict[int, dict[str, str]] = {}
            finish_reason = "stop"
            usage = TokenUsage()
            try:
                async for chunk in stream:
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage = TokenUsage(
                            prompt_tokens=int(getattr(chunk_usage, "prompt_tokens", 0) or 0),
                            completion_tokens=int(getattr(chunk_usage, "completion_tokens", 0) or 0),
                            total_tokens=int(getattr(chunk_usage, "total_tokens", 0) or 0),
                        )
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    choice = choices[0]
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    if getattr(delta, "content", None):
                        emitted = True
                        text_parts.append(delta.content)
                        yield TextDelta(text=delta.content)
                    for call_delta in getattr(delta, "tool_calls", None) or []:
                        index = int(getattr(call_delta, "index", 0) or 0)
                        slot = slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if getattr(call_delta, "id", None):
                            slot["id"] = call_delta.id
                        function = getattr(call_delta, "function", None)
                        if function is not None:
                            if getattr(function, "name", None):
                                slot["name"] += function.name
                            if getattr(function, "arguments", None):
                                slot["arguments"] += function.arguments
            except Exception as exc:  # noqa: BLE001
                error = map_openai_error(exc, self.name)
                if not emitted and attempt + 1 < attempts and error.retryable:
                    logger.warning("%s: stream failed before first token (%s), retrying", self.name, error.message)
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                raise error from exc
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # pragma: no cover - closing best effort
                        pass

            tool_calls = [
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"] or "unknown",
                    arguments=parse_tool_arguments(slot["arguments"]),
                    raw_arguments=slot["arguments"],
                )
                for index, slot in sorted(slots.items(), key=lambda item: item[0])
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
                finish_reason=finish_reason or "stop",
            )
            return
        raise ProviderError(f"{self.label}: retries exhausted", provider=self.name, retryable=True)

    # -------------------------------------------------------- embeddings
    async def embed(self, texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
        resolved = model or getattr(self, "default_embed_model", "text-embedding-3-small")
        try:
            response = await self._client.embeddings.create(model=resolved, input=list(texts))
        except Exception as exc:  # noqa: BLE001
            raise map_openai_error(exc, self.name) from exc
        items = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in items]

    # ------------------------------------------------------------ health
    async def list_models(self) -> list[str]:
        try:
            page = await self._client.models.list()
        except Exception as exc:  # noqa: BLE001
            raise map_openai_error(exc, self.name) from exc
        ids = [model.id for model in getattr(page, "data", []) if getattr(model, "id", None)]
        return ids or list(self.models)

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            ids = await self.list_models()
            latency = (time.perf_counter() - started) * 1000
            return ProviderHealth(
                ok=True,
                message=f"Connected · {len(ids)} model(s) available",
                latency_ms=latency,
                models=sorted(ids)[:50],
            )
        except ProviderError as exc:
            return ProviderHealth(
                ok=False,
                message=exc.message,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

    async def aclose(self) -> None:
        await self._client.close()


