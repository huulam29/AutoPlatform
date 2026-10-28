"""Exceptions a step can raise to steer the engine, plus engine-internal errors."""

from __future__ import annotations


class AgentlineError(Exception):
    pass


class PermanentError(AgentlineError):
    """Retrying won't help: the execution goes straight to dead."""


class NeedsReview(AgentlineError):
    """A human must decide: the execution goes to needs-review with this reason."""

    def __init__(self, reason: str, detail: dict[str, object] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class BudgetExceeded(NeedsReview):
    def __init__(self, spent: float, budget: float, scope: str = "run") -> None:
        super().__init__(
            "budget_exceeded", {"spent_usd": spent, "budget_usd": budget, "scope": scope}
        )
        self.spent = spent
        self.budget = budget


class StaleLease(AgentlineError):
    """This worker's lease was reclaimed; its writes are rejected (fencing)."""


class ValidationError(AgentlineError):
    pass
