"""Test registry for the CLI: the built-in registry plus a scripted `fake` runner, a
`fakebox` workspace and a `fake.flaky` action. Load with
`AGENTLINE_REGISTRY=tests.cli.fakes:registry`.

The fake runner picks its behavior from a `mode:<name>` word in the prompt:
- `ok` (default): output `{"summary": ...}`, cost from `with: {cost: ...}` (default 0.01)
- `missing`: reports `psql` as a missing tool
- `review`: raises NeedsReview("manual_check")
- `secret`: puts $AGENTLINE_TEST_TOKEN in its output, transcript and a log line
- `secretfail`: fails permanently with $AGENTLINE_TEST_TOKEN in the error
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agentline.builtins import default_registry
from agentline.config import Config
from agentline.context import StepContext
from agentline.errors import NeedsReview, PermanentError
from agentline.registry import ActionCall, Registry, RunRequest, RunResult, WorkspaceSpec
from agentline.runners import FunctionRunner
from agentline.runners.common import base_env, run_process

SECRET_ENV = "AGENTLINE_TEST_TOKEN"
log = logging.getLogger("agentline.test.fake")
FLAKY: dict[str, int] = {"calls": 0}


async def fake_agent(req: RunRequest) -> RunResult:
    m = re.search(r"mode:(\w+)", req.prompt)
    mode = m.group(1) if m else "ok"
    cost = float(req.params.get("cost", 0.01))
    if mode == "missing":
        return RunResult(
            output=None,
            transcript="bash: line 1: psql: command not found",
            missing_tools=["psql"],
            cost_usd=cost,
        )
    if mode == "review":
        raise NeedsReview("manual_check", {"note": "a human should look"})
    if mode in ("secret", "secretfail"):
        secret = os.environ[SECRET_ENV]
        log.warning("calling the API with token %s", secret)
        if mode == "secretfail":
            raise PermanentError(f"auth failed for token {secret}")
        return RunResult(
            output={"summary": f"the token is {secret}", "nested": [{"t": secret}]},
            transcript=f"$ curl -H 'Authorization: {secret}'\nok",
            cost_usd=cost,
        )
    return RunResult(
        output={"summary": f"done {req.node_id}#{req.iteration}"},
        transcript=f"worked on {req.node_id}",
        cost_usd=cost,
    )


class FakeBoxWorkspace:
    image_profile = "python"
    markers = ("pyproject.toml",)

    def __init__(self) -> None:
        self.path = Path(tempfile.gettempdir())

    async def exec(
        self, cmd: list[str], *, env: dict[str, str] | None = None, timeout: float | None = None
    ) -> tuple[int, str, str]:
        return await run_process(cmd, env={**base_env(), **(env or {})}, timeout=timeout)


class FakeBox:
    """A workspace that runs commands on the host (tests only) and reports a python image."""

    name = "fakebox"

    def acquire(
        self, ctx: StepContext, spec: WorkspaceSpec
    ) -> contextlib.AbstractAsyncContextManager[Any]:
        @contextlib.asynccontextmanager
        async def cm() -> AsyncIterator[FakeBoxWorkspace]:
            ctx.notes["image"] = {
                "ref": "agentline/img-app-000000000000",
                "source": "profile",
                "profile": "python",
                "markers": ["pyproject.toml"],
            }
            yield FakeBoxWorkspace()

        return cm()


def registry(config: Config | None = None) -> Registry:
    reg = default_registry(config)
    reg.add_runner(FunctionRunner("fake", fake_agent))
    reg.add_workspace(FakeBox())

    @reg.action("fake.flaky")
    async def flaky(ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        """An effect without key or check whose first call crashes after the intent."""

        async def do() -> dict[str, Any]:
            FLAKY["calls"] += 1
            if FLAKY["calls"] == 1:
                raise RuntimeError("connection reset while creating the thing")
            return {"id": FLAKY["calls"]}

        return await ctx.effect("create_thing", do)

    return reg
