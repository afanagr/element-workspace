"""Google Gemini provider (google-genai SDK): function calling + embeddings."""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, AsyncIterator, Sequence

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
)
from app.logging_config import get_logger

logger = get_logger(__name__)

GEMINI_MODELS = ["gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-pro", "gemini-1.5-flash"]
GEMINI_EMBEDDING_MODELS = ["text-embedding-004", "gemini-embedding-001"]

# Gemini rejects unknown JSON-Schema keywords, keep the supported subset only.
_ALLOWED_SCHEMA_KEYS = {
    "type",
    "format",
    "title",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "propertyOrdering",
}


def sanitize_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [sanitize_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _ALLOWED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            # Property names are arbitrary; only their schemas need sanitising.
            cleaned[key] = {name: sanitize_schema(schema) for name, schema in value.items()}
        elif key == "enum":
            cleaned[key] = list(value) if isinstance(value, (list, tuple)) else value
        else:
            cleaned[key] = sanitize_schema(value)
    if cleaned.get("type") == "object" and not cleaned.get("properties"):
        cleaned["properties"] = {}
    return cleaned


def _parse_data_url(url: str) -> tuple[bytes, str] | None:
    if not url.startswith("data:"):
        return None
    header, _, payload = url.partition(",")
    try:
        mime = header[5:].split(";")[0] or "image/png"
        return base64.b64decode(payload), mime
    except Exception:  # pragma: no cover - malformed client payload
        return None


class GeminiProvider(LLMProvider):
    name = "gemini"
    label = "Google Gemini"
    default_model = "gemini-2.0-flash"
    models = GEMINI_MODELS
    capabilities = ProviderCapabilities(
        supports_tools=True, supports_vision=True, supports_embeddings=True
    )

    def __init__(self, *, api_key: str, default_model: str | None = None) -> None:
        if not api_key:
            raise ProviderError("Gemini: API key is not configured", provider=self.name, code="missing_key")
        from google import genai
        from google.genai import types

        self.default_model = default_model or self.default_model
        self._types = types
        self._client = genai.Client(api_key=api_key)

    # ------------------------------------------------------------ helpers
    def _convert(self, messages: Sequence[ChatMessage]) -> tuple[str, list[Any]]:
        types = self._types
        system_parts: list[str] = []
        contents: list[Any] = []
        for message in messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
                continue
            if message.role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=message.name or "tool",
                                response={"result": message.content or ""},
                            )
                        ],
                    )
                )
                continue
            if message.role == "assistant":
                parts: list[Any] = []
                if message.content:
                    parts.append(types.Part.from_text(text=message.content))
                for call in message.tool_calls:
                    parts.append(types.Part.from_function_call(name=call.name, args=call.arguments))
                contents.append(
                    types.Content(role="model", parts=parts or [types.Part.from_text(text="(empty)")])
                )
                continue

            parts = []
            if message.content:
                parts.append(types.Part.from_text(text=message.content))
            for image in message.images:
                parsed = _parse_data_url(image)
                if parsed:
                    data, mime = parsed
                    parts.append(types.Part.from_bytes(data=data, mime_type=mime))
            contents.append(types.Content(role="user", parts=parts or [types.Part.from_text(text="")]))
        return "\n\n".join(system_parts), contents

    def _config(
        self,
        *,
        system: str,
        tools: Sequence[ToolSpec] | None,
        temperature: float,
        max_tokens: int | None,
    ) -> Any:
        types = self._types
        declarations = [
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=sanitize_schema(tool.parameters),
            )
            for tool in tools or []
        ]
        kwargs: dict[str, Any] = {"temperature": temperature}
        if system:
            kwargs["system_instruction"] = system
        if max_tokens:
            kwargs["max_output_tokens"] = max_tokens
        if declarations:
            kwargs["tools"] = [types.Tool(function_declarations=declarations)]
            kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _map_error(exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        try:
            status_int = int(status) if status is not None else None
        except (TypeError, ValueError):
            status_int = None
        message = str(getattr(exc, "message", None) or exc)
        return ProviderError(
            f"gemini: {message}",
            provider="gemini",
            status_code=status_int,
            retryable=is_retryable_status(status_int) if status_int else True,
            code="rate_limit" if status_int == 429 else None,
        )

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
        system, contents = self._convert(messages)
        config = self._config(
            system=system, tools=tools, temperature=temperature, max_tokens=max_tokens
        )
        attempts = max(1, settings.llm_max_retries + 1)
        for attempt in range(attempts):
            emitted = False
            text_parts: list[str] = []
            calls: list[ToolCall] = []
            usage = TokenUsage()
            finish_reason = "stop"
            try:
                stream = await self._client.aio.models.generate_content_stream(
                    model=resolved, contents=contents, config=config
                )
                async for chunk in stream:
                    meta = getattr(chunk, "usage_metadata", None)
                    if meta is not None:
                        usage = TokenUsage(
                            prompt_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
                            completion_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
                            total_tokens=int(getattr(meta, "total_token_count", 0) or 0),
                        )
                    candidates = getattr(chunk, "candidates", None) or []
                    if not candidates:
                        continue
                    candidate = candidates[0]
                    reason = getattr(candidate, "finish_reason", None)
                    if reason:
                        finish_reason = str(reason).lower()
                    content = getattr(candidate, "content", None)
                    for part in getattr(content, "parts", None) or []:
                        text = getattr(part, "text", None)
                        if text:
                            emitted = True
                            text_parts.append(text)
                            yield TextDelta(text=text)
                        function_call = getattr(part, "function_call", None)
                        if function_call is not None and getattr(function_call, "name", None):
                            calls.append(
                                ToolCall(
                                    id=f"call_{len(calls)}_{int(time.time() * 1000)}",
                                    name=function_call.name,
                                    arguments=dict(getattr(function_call, "args", None) or {}),
                                )
                            )
            except Exception as exc:  # noqa: BLE001
                error = self._map_error(exc)
                if not emitted and attempt + 1 < attempts and error.retryable:
                    logger.warning("gemini: stream failed before first token (%s), retrying", error.message)
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                raise error from exc

            for call in calls:
                yield ToolCallEvent(call=call)
            yield StreamEnd(
                message=ChatMessage(
                    role="assistant",
                    content="".join(text_parts) or None,
                    tool_calls=calls,
                ),
                usage=usage,
                finish_reason=finish_reason or "stop",
            )
            return
        raise ProviderError("gemini: retries exhausted", provider=self.name, retryable=True)

    # -------------------------------------------------------- embeddings
    async def embed(self, texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
        resolved = model or "text-embedding-004"
        try:
            response = await self._client.aio.models.embed_content(
                model=resolved, contents=list(texts)
            )
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        embeddings = getattr(response, "embeddings", None) or []
        vectors: list[list[float]] = []
        for item in embeddings:
            values = getattr(item, "values", None)
            if values is None:
                raise ProviderError("gemini: empty embedding returned", provider=self.name)
            vectors.append([float(value) for value in values])
        if not vectors:
            raise ProviderError("gemini: no embeddings returned", provider=self.name)
        return vectors

    # ------------------------------------------------------------ health
    async def list_models(self) -> list[str]:
        def _list() -> list[str]:
            names: list[str] = []
            for model in self._client.models.list():
                name = getattr(model, "name", "") or ""
                names.append(name.replace("models/", ""))
                if len(names) >= 100:
                    break
            return names

        try:
            return await asyncio.to_thread(_list)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            ids = await self.list_models()
            return ProviderHealth(
                ok=True,
                message=f"Connected · {len(ids)} model(s) available",
                latency_ms=(time.perf_counter() - started) * 1000,
                models=ids[:50],
            )
        except ProviderError as exc:
            return ProviderHealth(
                ok=False,
                message=exc.message,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

    async def aclose(self) -> None:
        closer = getattr(self._client, "aclose", None)
        if closer is None:
            closer = getattr(getattr(self._client, "aio", None), "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # pragma: no cover - closing best effort
                pass



