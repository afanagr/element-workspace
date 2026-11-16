"""Tool-call argument parsing: broken JSON must be reported, not hidden."""

from __future__ import annotations

from app.llm.schemas import parse_tool_arguments


def test_valid_arguments() -> None:
    arguments, error = parse_tool_arguments('{"node_id": "42", "max_chars": 100}')
    assert arguments == {"node_id": "42", "max_chars": 100}
    assert error is None


def test_empty_arguments() -> None:
    assert parse_tool_arguments(None) == ({}, None)
    assert parse_tool_arguments("") == ({}, None)


def test_broken_json_is_reported() -> None:
    arguments, error = parse_tool_arguments('{"node_id": ')
    assert arguments == {}
    assert error is not None
    assert "position" in error


def test_non_object_payload_is_reported() -> None:
    arguments, error = parse_tool_arguments("[1, 2, 3]")
    assert arguments == {}
    assert error == "arguments must be a JSON object"
