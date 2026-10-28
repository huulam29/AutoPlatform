"""Extension points: runners (agent nodes), actions, waiters (wait nodes), workspaces.

Pipelines name these by string (`runner: claude-code`, `uses: github.open_pr`,
`for: github.review`, `workspace: container`); a `Registry` maps names to implementations.
Each is written once and reused by every pipeline. Keep these interfaces small.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from agentline.errors import ValidationError

if TYPE_CHECKING:
    from agentline.config import Config
    from agentline.context import StepContext


# -- runners ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class Limits:
    allowed_tools: tuple[str, ...] = ("read",)
    max_turns: int | None = None
    budget_usd: float | None = None  # what's left for this call (min of node and run budget)
    timeout: float | None = None


@dataclass
class RunRequest:
    prompt: str
    inputs: dict[str, Any]
    limits: Limits
    workspace: Workspace | None = None
    params: dict[str, Any] = field(default_factory=dict)  # runner-specific (`with:`)
    run_id: str = ""
    node_id: str = ""
    iteration: int = 1


@dataclass
class RunResult:
    output: Any  # becomes the node's `out` artifact (JSON-serializable)
    transcript: str = ""
    cost_usd: float = 0.0
    missing_tools: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)  # e.g. turns, model, session id


class Runner(Protocol):
    name: str

    async def run(self, request: RunRequest) -> RunResult: ...


# -- workspaces ------------------------------------------------------------------------------
class Workspace(Protocol):
    path: Path  # the checkout/worktree the agent works in (inside: where it's mounted)

    async def exec(
        self, cmd: list[str], *, env: dict[str, str] | None = None, timeout: float | None = None
    ) -> tuple[int, str, str]: ...


@dataclass(frozen=True)
class WorkspaceSpec:
    type: str = "none"
    image: str = "auto"
    options: dict[str, Any] = field(default_factory=dict)


class WorkspaceProvider(Protocol):
    name: str

    def acquire(
        self, ctx: StepContext, spec: WorkspaceSpec
    ) -> contextlib.AbstractAsyncContextManager[Workspace]: ...


# -- actions ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class EffectPolicy:
    """Per-node override of an action's effect strategy (`effect:` in YAML)."""

    key: str | None = None
    on_unknown: Literal["halt", "retry", "ai"] | None = None


@dataclass
class ActionCall:
    params: dict[str, Any]  # `with:` after template rendering
    inputs: dict[str, Any]
    effect: EffectPolicy = field(default_factory=EffectPolicy)
    config: Config | None = None


class Action(Protocol):
    name: str
    deploy_class: bool

    async def __call__(self, ctx: StepContext, call: ActionCall) -> Any: ...


@dataclass
class FunctionAction:
    """Wrap a plain async function as an action."""

    name: str
    fn: Callable[[StepContext, ActionCall], Awaitable[Any]]
    deploy_class: bool = False

    async def __call__(self, ctx: StepContext, call: ActionCall) -> Any:
        return await self.fn(ctx, call)


# -- waiters ---------------------------------------------------------------------------------
class Waiter(Protocol):
    """A wait node parks on `correlation_key(...)`; a webhook or poller delivers the event."""

    name: str

    def correlation_key(self, params: dict[str, Any], inputs: dict[str, Any]) -> str: ...


@dataclass
class KeyWaiter:
    name: str
    key: Callable[[dict[str, Any], dict[str, Any]], str]

    def correlation_key(self, params: dict[str, Any], inputs: dict[str, Any]) -> str:
        return self.key(params, inputs)


class _NoWorkspace:
    name = "none"

    @contextlib.asynccontextmanager
    async def acquire(self, ctx: StepContext, spec: WorkspaceSpec) -> AsyncIterator[Workspace]:
        raise ValidationError("workspace 'none' cannot be acquired")
        yield  # pragma: no cover


class Registry:
    def __init__(self) -> None:
        self.runners: dict[str, Runner] = {}
        self.actions: dict[str, Action] = {}
        self.waiters: dict[str, Waiter] = {}
        self.workspaces: dict[str, WorkspaceProvider] = {}

    def add_runner(self, runner: Runner) -> Runner:
        self.runners[runner.name] = runner
        return runner

    def add_action(self, action: Action) -> Action:
        self.actions[action.name] = action
        return action

    def add_waiter(self, waiter: Waiter) -> Waiter:
        self.waiters[waiter.name] = waiter
        return waiter

    def add_workspace(self, provider: WorkspaceProvider) -> WorkspaceProvider:
        self.workspaces[provider.name] = provider
        return provider

    def action(
        self, name: str, *, deploy_class: bool = False
    ) -> Callable[[Callable[[StepContext, ActionCall], Awaitable[Any]]], FunctionAction]:
        """Decorator: register an async function as an action."""

        def register(fn: Callable[[StepContext, ActionCall], Awaitable[Any]]) -> FunctionAction:
            act = FunctionAction(name, fn, deploy_class)
            self.add_action(act)
            return act

        return register

    def get_runner(self, name: str) -> Runner:
        return _get(self.runners, name, "runner")

    def get_action(self, name: str) -> Action:
        return _get(self.actions, name, "action")

    def get_waiter(self, name: str) -> Waiter:
        return _get(self.waiters, name, "waiter")

    def get_workspace(self, name: str) -> WorkspaceProvider:
        return _get(self.workspaces, name, "workspace")

    def merged(self, other: Registry) -> Registry:
        out = Registry()
        for src in (self, other):
            out.runners.update(src.runners)
            out.actions.update(src.actions)
            out.waiters.update(src.waiters)
            out.workspaces.update(src.workspaces)
        return out


def _get(table: dict[str, Any], name: str, kind: str) -> Any:
    if name not in table:
        known = ", ".join(sorted(table)) or "none registered"
        raise ValidationError(f"unknown {kind} {name!r} (known: {known})")
    return table[name]
