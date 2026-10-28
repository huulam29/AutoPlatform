"""Webhook server: GitHub signature + replay + review -> wait resumed; Jira assign -> run."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline.api import create_app
from agentline.api.webhooks import jira_assignment, verify_github_signature
from agentline.errors import ValidationError
from agentline.registry import RunRequest
from tests.actions.conftest import Harness
from tests.actions.fakes import FakeState

from .conftest import GH_SECRET, JIRA_SECRET, gh_headers, review_event, sign

REVIEW_YAML = """
version: 1
name: review
nodes:
  make_pr: { type: agent, runner: fn, prompt: "go", in: [review?], out: pr }
  await_review: { type: wait, for: github.review, in: [pr], out: review }
  route: { type: router, switch: review.state }
  ship: { type: action, uses: shell.run, with: { cmd: "true" } }
edges:
  - make_pr -> await_review -> route
  - { from: route, to: make_pr, when: changes_requested, max_loops: 2 }
  - { from: route, to: ship, when: approved }
"""


async def pr_agent(req: RunRequest) -> dict[str, Any]:
    return {"repo": "acme/app", "number": 5, "branch": "agentline/x"}


def client(h: Harness, pipelines: list[Any], **kw: Any) -> TestClient:
    kw.setdefault("github_secret", GH_SECRET)
    kw.setdefault("jira_secret", JIRA_SECRET)
    kw.setdefault("github_client", lambda: None)
    return TestClient(create_app(h.app, pipelines, **kw))


def waiting(h: Harness, run: str) -> list[str]:
    return [x.correlation_key or "" for x in h.app.executions(run) if x.status == "waiting"]


def test_healthz(h: Harness) -> None:
    assert client(h, []).get("/healthz").json() == {"status": "ok"}


def test_signature_check() -> None:
    body = b'{"a": 1}'
    assert verify_github_signature("s", body, sign(body, "s"))
    assert not verify_github_signature("s", body, sign(body, "other"))
    assert not verify_github_signature("s", body + b" ", sign(body, "s"))
    assert not verify_github_signature("s", body, None)
    assert not verify_github_signature(None, body, sign(body, "s"))  # unset secret: reject


def test_github_bad_signature_is_401(h: Harness) -> None:
    c = client(h, [])
    body = review_event("approved")
    headers = gh_headers(body, "d1")
    headers["X-Hub-Signature-256"] = sign(body, "wrong")
    assert c.post("/webhooks/github", content=body, headers=headers).status_code == 401
    del headers["X-Hub-Signature-256"]
    assert c.post("/webhooks/github", content=body, headers=headers).status_code == 401
    unset = client(h, [], github_secret=None)
    r = unset.post("/webhooks/github", content=body, headers=gh_headers(body, "d1"))
    assert r.status_code == 401


def test_review_resumes_wait_and_replay_is_rejected(h: Harness) -> None:
    p = h.pipeline(REVIEW_YAML, agent=pr_agent)
    run = h.submit("review")
    h.drain()
    assert waiting(h, run) == ["github:pr:acme/app#5"]
    c = client(h, [p])

    body = review_event("approved", body="LGTM")
    r = c.post("/webhooks/github", content=body, headers=gh_headers(body, "delivery-1"))
    assert r.status_code == 202
    assert r.json() == {
        "status": "accepted",
        "correlation_key": "github:pr:acme/app#5",
        "run_ids": [run],
    }
    replay = c.post("/webhooks/github", content=body, headers=gh_headers(body, "delivery-1"))
    assert replay.status_code == 409
    h.drain()
    assert h.status(run) == "done"
    review = h.app.result(run, "await_review")
    assert review["state"] == "approved" and review["reviewer"] == "alice"
    assert review["comments"] == ["LGTM"] and review["commit_id"] == "c0ffee"
    assert review["source"] == "webhook"


def test_changes_requested_loops_and_commented_is_ignored(h: Harness) -> None:
    p = h.pipeline(REVIEW_YAML, agent=pr_agent)
    run = h.submit("review")
    h.drain()
    c = client(h, [p])
    body = review_event("commented", body="just a thought")
    r = c.post("/webhooks/github", content=body, headers=gh_headers(body, "d-c"))
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    h.drain()
    assert h.statuses(run)["await_review#1"] == "waiting"

    body = review_event("changes_requested", body="add tests", review_id=2)
    r = c.post("/webhooks/github", content=body, headers=gh_headers(body, "d-2"))
    assert r.status_code == 202
    h.drain()
    assert h.statuses(run)["await_review#2"] == "waiting"
    assert h.app.result(run, "await_review", 1)["state"] == "changes_requested"


def test_review_comments_fetched_when_token_present(
    h: Harness, fake: tuple[str, FakeState]
) -> None:
    _, state = fake
    from agentline.integrations import GitHubClient

    GitHubClient.from_env().create_pull("acme/app", head="x", base="main", title="t")
    for _ in range(4):
        GitHubClient.from_env().create_pull("acme/app", head=f"y{_}", base="main", title="t")
    review = state.add_review(
        "acme/app", 5, "changes_requested", comments=[{"body": "rename", "path": "a.py"}]
    )
    p = h.pipeline(REVIEW_YAML, agent=pr_agent)
    run = h.submit("review")
    h.drain()
    c = TestClient(create_app(h.app, [p], github_secret=GH_SECRET, jira_secret=JIRA_SECRET))
    body = review_event("changes_requested", review_id=review["id"], body="see inline")
    assert (
        c.post("/webhooks/github", content=body, headers=gh_headers(body, "d")).status_code == 202
    )
    assert h.app.result(run, "await_review", 1)["comments"] == ["see inline", "a.py: rename"]


def test_event_before_wait_exists_is_not_consumed(h: Harness) -> None:
    """Engine rule (Phase 1): a wait consumes events received after its execution was
    created. A review that lands before then is kept but not consumed; the poller (head-sha
    match) is what picks such a review up."""
    p = h.pipeline(REVIEW_YAML, agent=pr_agent)
    c = client(h, [p])
    run = h.submit("review")
    body = review_event("approved")
    r = c.post("/webhooks/github", content=body, headers=gh_headers(body, "early"))
    assert r.status_code == 202 and r.json()["run_ids"] == []
    h.drain()
    assert h.status(run) == "waiting"


def test_other_github_events(h: Harness) -> None:
    c = client(h, [])
    body = b"{}"
    assert c.post(
        "/webhooks/github", content=body, headers=gh_headers(body, "p", "ping")
    ).json() == {"status": "pong"}
    r = c.post("/webhooks/github", content=body, headers=gh_headers(body, "p2", "push"))
    assert r.status_code == 200 and r.json()["status"] == "ignored"


# -- jira -----------------------------------------------------------------------------------
JIRA_YAML = """
version: 1
name: {name}
trigger: {{ type: webhook, source: jira, event: issue_assigned{filter} }}
nodes:
  step: {{ type: action, uses: shell.run, with: {{ cmd: "true" }} }}
"""


def jira_event(
    key: str = "PROJ-1",
    *,
    account: str | None = "acc-1",
    email: str = "dev@example.com",
    changelog_id: str = "100",
    field: str = "assignee",
) -> dict[str, Any]:
    assignee = {"accountId": account, "emailAddress": email, "displayName": "Dev"}
    return {
        "webhookEvent": "jira:issue_updated",
        "issue_event_type_name": "issue_updated",
        "timestamp": 1700000000000,
        "issue": {
            "key": key,
            "fields": {
                "assignee": assignee if account else None,
                "project": {"key": key.split("-")[0]},
            },
        },
        "changelog": {"id": changelog_id, "items": [{"field": field, "to": account}]},
    }


def post_jira(c: TestClient, event: dict[str, Any], secret: str = JIRA_SECRET) -> Any:
    return c.post("/webhooks/jira", json=event, headers={"X-Agentline-Secret": secret})


def test_jira_secret(h: Harness) -> None:
    p = h.pipeline(JIRA_YAML.format(name="t", filter=""))
    c = client(h, [p])
    assert post_jira(c, jira_event(), secret="nope").status_code == 401
    assert c.post("/webhooks/jira", json=jira_event()).status_code == 401
    r = c.post(f"/webhooks/jira?secret={JIRA_SECRET}", json=jira_event())
    assert r.status_code == 202


def test_jira_assign_submits_run_and_dedupes(h: Harness) -> None:
    p = h.pipeline(JIRA_YAML.format(name="t", filter=", filter: { project: PROJ }"))
    c = client(h, [p])
    r = post_jira(c, jira_event())
    assert r.status_code == 202
    (run_id,) = r.json()["run_ids"]
    run = h.app.get_run(run_id)
    assert run is not None
    assert run.payload == {"issue_key": "PROJ-1", "assignee": "acc-1", "event": "issue_assigned"}
    assert run.dedupe_key == "jira:PROJ-1:assigned:acc-1:100"

    dup = post_jira(c, jira_event())
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate"
    assert dup.json()["runs"][0]["run_id"] == run_id
    again = post_jira(c, jira_event(changelog_id="101"))  # a later re-assignment
    assert again.status_code == 202 and again.json()["run_ids"] != [run_id]


def test_jira_filter_mismatch_and_non_assignments_are_ignored(h: Harness) -> None:
    p = h.pipeline(
        JIRA_YAML.format(
            name="t", filter=", filter: { project: [PROJ, OPS], assignee: dev@example.com }"
        )
    )
    c = client(h, [p])
    r = post_jira(c, jira_event("OTHER-3"))
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert post_jira(c, jira_event(email="someone@else.com")).json()["status"] == "ignored"
    assert post_jira(c, jira_event(field="status")).json()["status"] == "ignored"
    assert post_jira(c, jira_event(account=None)).json()["status"] == "ignored"  # unassigned
    assert post_jira(c, jira_event("OPS-2")).status_code == 202


def test_jira_issue_assigned_event_type(h: Harness) -> None:
    p = h.pipeline(JIRA_YAML.format(name="t", filter=""))
    c = client(h, [p])
    event = jira_event()
    event["issue_event_type_name"] = "issue_assigned"
    event["changelog"] = {}
    r = post_jira(c, event)
    assert r.status_code == 202
    run = h.app.get_run(r.json()["run_ids"][0])
    assert run is not None and run.dedupe_key == "jira:PROJ-1:assigned:acc-1:1700000000000"


def test_jira_two_pipelines_get_separate_runs(h: Harness) -> None:
    a = h.pipeline(JIRA_YAML.format(name="a", filter=""))
    b = h.pipeline(JIRA_YAML.format(name="b", filter=""))
    manual = h.pipeline(
        "version: 1\nname: m\nnodes:\n  s: { type: action, uses: shell.run, "
        'with: { cmd: "true" } }\n'
    )
    c = client(h, [a, b, manual])
    r = post_jira(c, jira_event())
    assert r.status_code == 202
    assert sorted(x["pipeline"] for x in r.json()["runs"]) == ["a", "b"]
    assert len(set(r.json()["run_ids"])) == 2


def test_unknown_filter_key_is_rejected(h: Harness) -> None:
    p = h.pipeline(JIRA_YAML.format(name="t", filter=", filter: { projekt: PROJ }"))
    with pytest.raises(ValidationError, match="projekt"):
        create_app(h.app, [p], github_secret="x", jira_secret="y")


def test_jira_assignment_parsing() -> None:
    a = jira_assignment(jira_event())
    assert a is not None and a["identities"] == {"acc-1", "dev@example.com", "Dev"}
    assert jira_assignment({"webhookEvent": "jira:issue_created", "issue": {"key": "X-1"}}) is None
    assert json.dumps(jira_event())  # sanity: the fixture is plain JSON
