"""The same ticket job as demo/naive, on the durable engine: plan (LLM) -> open_pr (effect).

Env vars (same contract as the naive worker, so demo.chaos can drive both):
    LEDGER_DB      ledger: a SQLite path, sqlite:///path or postgresql://... (required)
    FAKES_URL      base URL of demo.fakes.server (required)
    CONCURRENCY    worker slots (default 4)
    LEASE_TTL      seconds (default 1.0)
    EXIT_WHEN_DONE "1" -> exit once nothing is claimable
    CRASH_AT       after_llm | after_pr_post | after_pr -> os._exit(137) there (tests)
    CRASH_MARKER   with CRASH_AT: crash only once
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from agentline import App, StepContext
from agentline.ledger import ledger_from_url
from demo.fakes.server import get_json, post_json
from demo.naive.worker import crash_point


def build_app(ledger_db: str, fakes_url: str) -> App:
    app = App(ledger_from_url(ledger_db), backoff_base=0.2)
    flow = app.flow("ticket", budget_usd=1.0)

    @flow.step("plan")
    async def plan(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        ticket = inputs["payload"]["ticket"]
        await ctx.check_budget(0.01)
        out = await asyncio.to_thread(
            post_json, f"{fakes_url}/llm", {"job_id": ticket, "prompt": f"plan {ticket}"}
        )
        await ctx.spend(out["cost_usd"], "llm")
        crash_point("after_llm")
        return {"text": out["text"], "branch": f"agentline/{ticket}"}

    @flow.step("open_pr", after="plan")
    async def open_pr(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        ticket = inputs["payload"]["ticket"]
        branch = inputs["plan"]["branch"]

        async def find() -> dict[str, Any] | None:
            prs = await asyncio.to_thread(get_json, f"{fakes_url}/prs?branch={branch}")
            return prs[0] if prs else None

        async def create() -> dict[str, Any]:
            body = {"ticket": ticket, "branch": branch, "title": inputs["plan"]["text"][:60]}
            pr = await asyncio.to_thread(post_json, f"{fakes_url}/prs", body)
            crash_point("after_pr_post")  # PR exists, effect not yet committed
            return pr  # type: ignore[no-any-return]

        pr = await ctx.effect("create_pr", create, check=find, description=f"PR for {branch}")
        crash_point("after_pr")
        return {"number": pr["number"]}

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
