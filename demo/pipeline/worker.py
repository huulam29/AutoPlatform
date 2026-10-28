"""examples/ticket-to-eod on the engine, with a scripted fake agent and fake Jira/GitHub/deploy
backed by demo.fakes.server, so the chaos harness can count every external effect.

Env vars: LEDGER_DB, FAKES_URL, CONCURRENCY, LEASE_TTL, EXIT_WHEN_DONE (as demo/durable).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from agentline import App, StepContext
from agentline.config import Config
from agentline.ledger import ledger_from_url
from agentline.pipeline import load_pipeline
from agentline.registry import ActionCall, KeyWaiter, Registry, RunRequest, RunResult
from agentline.runners import FunctionRunner
from demo.fakes.server import get_json, post_json

PIPELINE = Path(__file__).resolve().parents[2] / "examples" / "ticket-to-eod" / "pipeline.yaml"


def fake_registry(fakes_url: str) -> Registry:
    reg = Registry()

    async def agent(req: RunRequest) -> RunResult:
        ticket = req.inputs["ticket"]["key"]
        job = f"{ticket}:{req.node_id}#{req.iteration}"
        out = await asyncio.to_thread(
            post_json, f"{fakes_url}/llm", {"job_id": job, "prompt": req.prompt}
        )
        if req.node_id == "plan":
            output: Any = {
                "summary": f"Fix {ticket}",
                "approach": out["text"],
                "steps": ["edit", "test"],
            }
        else:
            output = {"name": f"agentline/{ticket}", "sha": f"{ticket}-v{req.iteration}"}
        return RunResult(output=output, cost_usd=out["cost_usd"], transcript=out["text"])

    reg.add_runner(FunctionRunner("claude-code", agent))

    @reg.action("jira.get_ticket")
    async def get_ticket(ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        key = call.params["key"]
        return {"key": key, "summary": f"Ticket {key}", "description": "Login is broken."}

    @reg.action("github.open_pr")
    async def open_pr(ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        ticket = call.inputs["ticket"]["key"]
        branch = call.inputs["branch"]["name"]

        async def find() -> dict[str, Any] | None:
            prs = await asyncio.to_thread(get_json, f"{fakes_url}/prs?branch={branch}")
            return prs[0] if prs else None

        async def find_or_create() -> dict[str, Any]:  # idempotent by branch across iterations
            if existing := await find():
                return existing
            body = {"ticket": ticket, "branch": branch, "title": call.params.get("title", "")}
            return await asyncio.to_thread(post_json, f"{fakes_url}/prs", body)  # type: ignore[no-any-return]

        return await ctx.effect("open_pr", find_or_create, check=find)

    @reg.action("env.deploy", deploy_class=True)
    async def deploy(ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        ticket = ctx.payload["issue_key"]
        sha = call.inputs["branch"]["sha"]
        env = call.params["env"]

        async def find() -> dict[str, Any] | None:
            deps = await asyncio.to_thread(get_json, f"{fakes_url}/deploys?ticket={ticket}")
            return next((d for d in deps if d["sha"] == sha and d["env"] == env), None)

        async def do() -> dict[str, Any]:
            body = {"ticket": ticket, "env": env, "sha": sha}
            return await asyncio.to_thread(post_json, f"{fakes_url}/deploys", body)  # type: ignore[no-any-return]

        return await ctx.effect("deploy", do, check=find, deploy_class=True)

    @reg.action("env.test")
    async def test(ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        return {"passed": True, "env": call.params["env"], "sha": call.inputs["deployment"]["sha"]}

    reg.add_waiter(
        KeyWaiter("github.review", lambda params, inputs: f"github:pr:{inputs['pr']['number']}")
    )

    class NoopWorkspaces:
        name = "container"

        def acquire(self, ctx: Any, spec: Any) -> Any:
            import contextlib

            @contextlib.asynccontextmanager
            async def cm() -> Any:
                yield None

            return cm()

    reg.add_workspace(NoopWorkspaces())
    return reg


def build_app(ledger_db: str, fakes_url: str) -> App:
    app = App(ledger_from_url(ledger_db), backoff_base=0.2)
    # the example names `harness: claude-code` (faked here); only builtin is built in
    cfg = Config.from_dict({"harnesses": {"claude-code": {"kind": "claude-code"}}})
    app.register_pipeline(load_pipeline(PIPELINE, fake_registry(fakes_url), cfg))
    return app


async def run() -> None:
    app = build_app(os.environ["LEDGER_DB"], os.environ["FAKES_URL"])
    worker = app.worker(
        concurrency=int(os.environ.get("CONCURRENCY", "4")),
        lease_ttl=float(os.environ.get("LEASE_TTL", "1.0")),
        poll_interval=0.05,
    )
    if os.environ.get("EXIT_WHEN_DONE") == "1":
        await worker.run_until_idle()
    else:
        await worker.run()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
