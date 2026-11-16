"""Provider retry back-off honours ``Retry-After`` and clamps it."""

from __future__ import annotations

from types import SimpleNamespace

from app.llm.openai_provider import retry_delay


def _error_with_header(value: str) -> Exception:
    error = Exception("boom")
    error.response = SimpleNamespace(headers={"retry-after": value})  # type: ignore[attr-defined]
    return error


def test_retry_after_is_used() -> None:
    assert retry_delay(_error_with_header("3"), 0) == 3.0


def test_retry_after_is_clamped() -> None:
    assert retry_delay(_error_with_header("600"), 0) == 30.0


def test_invalid_retry_after_falls_back() -> None:
    assert retry_delay(_error_with_header("soon"), 1) == 1.6


def test_missing_response_uses_exponential_backoff() -> None:
    assert retry_delay(Exception("no response"), 2) == 2.4
