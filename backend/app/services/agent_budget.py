"""Budget of a single agent run: steps, wall clock and context tokens."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.config import settings
from app.llm.schemas import TokenUsage

# Rough characters-per-token ratio of mixed Russian/English text. Only used to
# decide when to compact the history; the provider's own counters are charged
# when they are available.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str | None) -> int:
    """Cheap token estimate for one string (never zero for non-empty text)."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass(slots=True)
class AgentBudget:
    """Tracks how much of one run is already spent."""

    max_steps: int = field(default_factory=lambda: max(1, settings.max_agent_steps))
    deadline_seconds: float = field(default_factory=lambda: settings.agent_deadline_seconds)
    token_budget: int = field(default_factory=lambda: settings.agent_token_budget)
    started: float = field(default_factory=time.perf_counter)
    tokens_used: int = 0
    steps_used: int = 0

    @property
    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self.started

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.deadline_seconds - self.elapsed_seconds)

    @property
    def tokens_left(self) -> int:
        return max(0, self.token_budget - self.tokens_used)

    def expired(self) -> bool:
        return self.seconds_left <= 0 or self.tokens_left <= 0

    def next_step(self) -> bool:
        """Reserve the next step; ``False`` when the run must stop."""
        if self.expired() or self.steps_used >= self.max_steps:
            return False
        self.steps_used += 1
        return True

    def charge(self, usage: TokenUsage | None = None, *, extra_text: str | None = None) -> None:
        """Account the provider's usage (falling back to an estimate)."""
        if usage is not None and (usage.prompt_tokens or usage.completion_tokens):
            self.tokens_used += max(0, usage.prompt_tokens) + max(0, usage.completion_tokens)
            return
        self.tokens_used += estimate_tokens(extra_text)
