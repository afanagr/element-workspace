"""The advertised tool set and the dispatcher must stay in sync."""

from __future__ import annotations

from app.services.agent_prompt import MUTATING_TOOLS
from app.services.tools import TOOL_NAMES, _HANDLERS, tool_specs


def test_every_tool_has_a_handler() -> None:
    assert TOOL_NAMES == set(_HANDLERS)


def test_mutating_tools_are_real_tools() -> None:
    assert MUTATING_TOOLS <= TOOL_NAMES


def test_new_tools_are_advertised() -> None:
    names = {spec.name for spec in tool_specs()}
    assert {"find_nodes", "read_file", "edit_file", "append_file"} <= names


def test_specs_have_required_parameters() -> None:
    for spec in tool_specs():
        if spec.name in {"list_workspace", "search_documents"}:
            continue
        assert spec.parameters.get("type") == "object"
        assert spec.parameters.get("properties"), spec.name
