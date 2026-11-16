"""Unit tests for the agent run budget (no network, no database)."""

from __future__ import annotations

from app.llm.schemas import TokenUsage
from app.services.agent_budget import AgentBudget, estimate_tokens


def test_estimate_tokens_handles_empty_and_text() -> None:
    assert estimate_tokens(None) == 0
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_steps_are_bounded() -> None:
    budget = AgentBudget(max_steps=2, deadline_seconds=60, token_budget=10_000)
    assert budget.next_step() is True
    assert budget.next_step() is True
    assert budget.next_step() is False


def test_usage_is_charged_against_the_token_budget() -> None:
    budget = AgentBudget(max_steps=5, deadline_seconds=60, token_budget=100)
    budget.charge(TokenUsage(prompt_tokens=60, completion_tokens=40, total_tokens=100))
    assert budget.tokens_left == 0
    assert budget.expired()
    assert budget.next_step() is False


def test_charge_falls_back_to_an_estimate() -> None:
    budget = AgentBudget(max_steps=5, deadline_seconds=60, token_budget=1000)
    budget.charge(TokenUsage(), extra_text="x" * 400)
    assert budget.tokens_used == 100


def test_deadline_is_enforced() -> None:
    budget = AgentBudget(max_steps=5, deadline_seconds=10, token_budget=1000)
    budget.started -= 20
    assert budget.seconds_left == 0
    assert budget.expired()
    assert budget.next_step() is False
