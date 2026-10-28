"""Special values a step handler can return instead of a plain result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Wait:
    """Park this execution until an event with `correlation_key` is delivered.
    The event payload becomes the execution's result; with `detail["resume"]` the execution
    runs again instead (a conversation: agentline.runchat)."""

    correlation_key: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Route:
    """Router result: follow the out-edge whose `when` equals `label`."""

    label: str
    output: dict[str, Any] = field(default_factory=dict)

    def as_result(self) -> dict[str, Any]:
        return {**self.output, "route": self.label}
