"""A runner backed by an in-process async function: fake agents in tests and demos."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agentline.registry import RunRequest, RunResult


@dataclass
class FunctionRunner:
    name: str
    fn: Callable[[RunRequest], Awaitable[RunResult | Any]]

    async def run(self, request: RunRequest) -> RunResult:
        out = await self.fn(request)
        return out if isinstance(out, RunResult) else RunResult(output=out)
