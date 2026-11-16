"""Agent-side tool plumbing: signatures and JSON-parse error handling."""

from __future__ import annotations

from types import SimpleNamespace

from app.llm.schemas import ToolCall
from app.services.agent import _call_signature, _call_tool


def test_signature_ignores_key_order() -> None:
    first = _call_signature("read_file", {"node_id": "1", "max_chars": 10})
    second = _call_signature("read_file", {"max_chars": 10, "node_id": "1"})
    assert first == second


def test_signature_differs_for_other_arguments() -> None:
    first = _call_signature("read_file", {"node_id": "1"})
    second = _call_signature("read_file", {"node_id": "2"})
    assert first != second


async def test_broken_json_is_reported_without_running_the_tool() -> None:
    call = ToolCall(
        id="call_1",
        name="read_file",
        arguments={},
        raw_arguments="{bad",
        parse_error="Expecting value at position 1",
    )
    context = SimpleNamespace(db=None, project_id="p", user=None)
    result = await _call_tool(context, call)  # type: ignore[arg-type]
    assert result.ok is False
    assert "not valid JSON" in result.summary
    assert "read_file" in result.summary
