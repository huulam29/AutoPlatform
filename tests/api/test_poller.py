"""The poller resumes github.review waits when no webhook comes, and never double-delivers."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi.testclient import TestClient

from agentline.api import create_app
from agentline.poller import PollState, poll_once, run_poller
from agentline.registry import RunRequest
from tests.actions.conftest import Harness
from tests.actions.fakes import FakeState

from .conftest import GH_SECRET, gh_headers, review_event

POLL_YAML = """
version: 1
name: polled
nodes:
  make_pr: { type: agent, runner: fn, prompt: "go", in: [review?], out: pr }
  await_review: { type: wait, for: github.review, in: [pr], out: review, poll_every: 60 }
  route: { type: router, switch: review.state }
  ship: { type: action, uses: shell.run, with: { cmd: "true" } }
edges:
  - make_pr -> await_review -> route
  - { from: route, to: make_pr, when: changes_requested, max_loops: 2 }
  - { from: route, to: ship, when: approved }
"""


def pr_agent(state: FakeState) -> Any:
    async def agent(req: RunRequest) -> dict[str, Any]:
        sha = f"sha{req.iteration}"
        with state.lock:
            state.prs.setdefault("acme/app", [{"number": 1, "state": "open", "head": {}}])
        return {"repo": "acme/app", "number": 1, "head_sha": sha}

    return agent


def later(seconds: float = 61) -> float:
    return time.time() + seconds


def test_poller_delivers_when_no_webhook_comes(h: Harness, fake: tuple[str, FakeState]) -> None:
    _, state = fake
    h.pipeline(POLL_YAML, agent=pr_agent(state))
    run = h.submit("polled")
    h.drain()
    assert h.registry is not None
    ps = PollState()
    assert asyncio.run(poll_once(h.app, h.registry, ps)) == []  # not due yet
    assert asyncio.run(poll_once(h.app, h.registry, ps, now=later())) == []  # due, no review
    polls = sum(1 for _, path in state.requests if path.endswith("/reviews?per_page=100"))
    assert polls == 1

    state.add_review("acme/app", 1, "changes_requested", commit_id="sha1", body="fix")
    assert asyncio.run(poll_once(h.app, h.registry, ps, now=later(62))) == []  # 1 s later: not due
    assert asyncio.run(poll_once(h.app, h.registry, ps, now=later(122))) == ["github:pr:acme/app#1"]
    h.drain()
    assert h.app.result(run, "await_review", 1)["source"] == "poll"
    assert h.statuses(run)["await_review#2"] == "waiting"

    # iteration 2 waits for a review of sha2; the old sha1 review is not delivered again
    assert asyncio.run(poll_once(h.app, h.registry, PollState(), now=later(300))) == []
    state.add_review("acme/app", 1, "approved", commit_id="sha2")
    assert len(asyncio.run(poll_once(h.app, h.registry, PollState(), now=later(300)))) == 1
    h.drain()
    assert h.status(run) == "done"


def test_late_webhook_after_poll_does_not_resume_next_iteration(
    h: Harness, fake: tuple[str, FakeState]
) -> None:
    _, state = fake
    p = h.pipeline(POLL_YAML, agent=pr_agent(state))
    run = h.submit("polled")
    h.drain()
    assert h.registry is not None
    review = state.add_review("acme/app", 1, "changes_requested", commit_id="sha1")
    assert len(asyncio.run(poll_once(h.app, h.registry, now=later()))) == 1
    h.drain()
    assert h.statuses(run)["await_review#2"] == "waiting"

    c = TestClient(create_app(h.app, [p], github_secret=GH_SECRET, github_client=lambda: None))
    body = review_event("changes_requested", repo="acme/app", number=1, review_id=review["id"])
    r = c.post("/webhooks/github", content=body, headers=gh_headers(body, "late"))
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    h.drain()
    assert h.statuses(run)["await_review#2"] == "waiting"


def test_waits_without_poll_every_are_not_polled(h: Harness, fake: tuple[str, FakeState]) -> None:
    _, state = fake
    h.pipeline(POLL_YAML.replace(", poll_every: 60", ""), agent=pr_agent(state))
    h.submit("polled")
    h.drain()
    assert h.registry is not None
    state.add_review("acme/app", 1, "approved", commit_id="sha1")
    assert asyncio.run(poll_once(h.app, h.registry, now=later(10_000))) == []


def test_poll_errors_are_survived(
    h: Harness, fake: tuple[str, FakeState], monkeypatch: Any
) -> None:
    _, state = fake
    h.pipeline(POLL_YAML, agent=pr_agent(state))
    run = h.submit("polled")
    h.drain()
    assert h.registry is not None
    monkeypatch.setenv("GITHUB_TOKEN", "wrong-token")
    assert asyncio.run(poll_once(h.app, h.registry, now=later())) == []
    assert h.status(run) == "waiting"


def test_run_poller_loop(h: Harness, fake: tuple[str, FakeState]) -> None:
    _, state = fake
    h.pipeline(POLL_YAML.replace("poll_every: 60", "poll_every: 0.05"), agent=pr_agent(state))
    run = h.submit("polled")
    h.drain()
    assert h.registry is not None
    state.add_review("acme/app", 1, "approved", commit_id="sha1")

    async def main() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_poller(h.app, h.registry, stop, interval=0.02))  # type: ignore[arg-type]
        for _ in range(200):
            if h.statuses(run)["await_review#1"] == "done":
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(main())
    h.drain()
    assert h.status(run) == "done"
