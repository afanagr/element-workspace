"""Provider-agnostic message / tool / streaming schemas.

Every provider converts to and from these structures, so the agent loop never
knows which API produced a token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCall":
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            arguments=dict(data.get("arguments") or {}),
        )


@dataclass(slots=True)
class ChatMessage:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # Data URLs (data:image/png;base64,...) or plain https URLs for vision models.
    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "name": self.name,
        }


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(slots=True)
class TextDelta:
    text: str
    type: Literal["text_delta"] = "text_delta"


@dataclass(slots=True)
class ToolCallEvent:
    call: ToolCall
    type: Literal["tool_call"] = "tool_call"


@dataclass(slots=True)
class StreamEnd:
    message: ChatMessage
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str = "stop"
    type: Literal["stream_end"] = "stream_end"


StreamEvent = Union[TextDelta, ToolCallEvent, StreamEnd]


class ProviderError(Exception):
    """Normalised provider failure with enough context for the UI to react."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
        retryable: bool = False,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.code = code

    @property
    def is_rate_limit(self) -> bool:
        return self.status_code == 429 or self.code == "rate_limit"

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "provider": self.provider,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "code": self.code,
        }


class NoProviderAvailable(Exception):
    """Raised when no configured provider can serve the request."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


def is_retryable_status(status_code: int | None) -> bool:
    return status_code is None or status_code == 429 or status_code >= 500


def parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    import json

    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
