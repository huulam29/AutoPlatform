"""Fake runner/actions/waiters registered under the real names used by the example pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentline.context import StepContext
from agentline.registry import ActionCall, KeyWaiter, Registry, RunRequest, RunResult
from agentline.runners import FunctionRunner


@dataclass
class World:
    prs: dict[str, dict[str, Any]] = field(default_factory=dict)  # branch -> PR
    pr_creates: int = 0
    deploys: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    agent_calls: list[tuple[str, int]] = field(default_factory=list)
    prompts: dict[str, str] = field(default_factory=dict)
    plan_outputs: list[Any] = field(default_factory=list)  # scripted plan outputs (else valid)
    missing_tools: list[str] = field(default_factory=list)


def make_registry(world: World) -> Registry:
    reg = Registry()

    async def agent(req: RunRequest) -> RunResult:
        world.agent_calls.append((req.node_id, req.iteration))
        world.prompts[f"{req.node_id}#{req.iteration}"] = req.prompt
        if world.missing_tools:
            return RunResult(
                output=None, missing_tools=world.missing_tools, transcript="psql: not found"
            )
        if req.node_id == "plan":
            if world.plan_outputs:
                return RunResult(output=world.plan_outputs.pop(0), cost_usd=0.01)
            return RunResult(
                output={"summary": "Fix login", "approach": "patch it", "steps": ["edit", "test"]},
                cost_usd=0.01,
                transcript="planned",
            )
        key = req.inputs["ticket"]["key"]
        return RunResult(
            output={"name": f"agentline/{key}", "sha": f"sha{req.iteration}"},
            cost_usd=0.05,
            transcript=f"implemented v{req.iteration}",
        )

    reg.add_runner(FunctionRunner("claude-code", agent))

    @reg.action("jira.get_ticket")
    async def get_ticket(ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        key = call.params["key"]
        return {"key": key, "summary": f"Summary of {key}", "description": "It is broken."}

    @reg.action("github.open_pr")
    async def open_pr(ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        branch = call.inputs["branch"]["name"]

        async def find() -> dict[str, Any] | None:
            return world.prs.get(branch)

        async def create() -> dict[str, Any]:
            if branch in world.prs:  # idempotent by branch: reuse the open PR
                return world.prs[branch]
            world.pr_creates += 1
            world.prs[branch] = {
                "number": len(world.prs) + 1,
                "branch": branch,
                "title": call.params.get("title", ""),
            }
            return world.prs[branch]

        return await ctx.effect("open_pr", create, check=find)

    @reg.action("env.deploy", deploy_class=True)
    async def deploy(ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        async def do() -> dict[str, Any]:
            sha = call.inputs.get("branch", {}).get("sha")
            world.deploys.append({"env": call.params["env"], "sha": sha})
            return {"env": call.params["env"], "sha": sha}

        return await ctx.effect("deploy", do, deploy_class=True)

    @reg.action("env.test")
    async def test(ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        world.tests.append(call.inputs.get("deployment", {}))
        return {"passed": True, "env": call.params["env"]}

    reg.add_waiter(
        KeyWaiter("github.review", lambda params, inputs: f"github:pr:{inputs['pr']['number']}")
    )

    class FakeWorkspaces:
        name = "container"

        def acquire(self, ctx: Any, spec: Any) -> Any:
            import contextlib
            from pathlib import Path

            @contextlib.asynccontextmanager
            async def cm() -> Any:
                class WS:
                    path = Path("/tmp/fake-worktree")

                    async def exec(self, cmd: list[str], **kw: Any) -> tuple[int, str, str]:
                        return 0, "", ""

                yield WS()

            return cm()

    reg.add_workspace(FakeWorkspaces())
    return reg
