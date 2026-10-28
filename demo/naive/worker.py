"""Naive at-least-once worker: claim -> LLM -> POST /prs -> ack.

Nothing is journaled. If the process dies after the LLM call, the call is repeated on
redelivery. If it dies after the PR is created but before the ack, a second PR is opened.

Configured by env vars so the chaos harness can run any worker command the same way:
    QUEUE_DB            path to the job queue SQLite file (required)
    FAKES_URL           base URL of demo.fakes.server (required)
    CONCURRENCY         concurrent job loops (default 4)
    VISIBILITY_TIMEOUT  seconds before an unacked job is redelivered (default 2)
    EXIT_WHEN_DONE      "1" -> exit once every job is done
    CRASH_AT            "after_llm" | "after_pr" -> os._exit(137) at that point (tests)
    CRASH_MARKER        with CRASH_AT: crash only if this file doesn't exist yet, then create it
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from demo.fakes.server import post_json
from demo.naive.queue import Job, JobQueue


def crash_point(name: str) -> None:
    if os.environ.get("CRASH_AT") != name:
        return
    marker = os.environ.get("CRASH_MARKER")
    if marker:
        if Path(marker).exists():
            return
        Path(marker).touch()
    os._exit(137)


async def process(job: Job, queue: JobQueue, fakes_url: str) -> None:
    llm = await asyncio.to_thread(
        post_json, f"{fakes_url}/llm", {"job_id": job.ticket, "prompt": f"plan {job.ticket}"}
    )
    crash_point("after_llm")
    await asyncio.to_thread(
        post_json,
        f"{fakes_url}/prs",
        {"ticket": job.ticket, "branch": f"agentline/{job.ticket}", "title": llm["text"][:60]},
    )
    crash_point("after_pr")
    await asyncio.to_thread(queue.ack, job.id)


async def job_loop(queue: JobQueue, fakes_url: str, exit_when_done: bool) -> None:
    while True:
        job = await asyncio.to_thread(queue.claim)
        if job is None:
            if exit_when_done and await asyncio.to_thread(queue.all_done):
                return
            await asyncio.sleep(0.05)
            continue
        await process(job, queue, fakes_url)


async def run() -> None:
    queue = JobQueue(
        os.environ["QUEUE_DB"], visibility_timeout=float(os.environ.get("VISIBILITY_TIMEOUT", "2"))
    )
    fakes_url = os.environ["FAKES_URL"]
    exit_when_done = os.environ.get("EXIT_WHEN_DONE") == "1"
    concurrency = int(os.environ.get("CONCURRENCY", "4"))
    await asyncio.gather(*(job_loop(queue, fakes_url, exit_when_done) for _ in range(concurrency)))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
