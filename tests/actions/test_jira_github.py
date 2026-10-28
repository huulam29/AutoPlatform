"""jira.* and github.* actions on the engine, against the fake servers and a local git remote."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentline.actions.github import ReviewWaiter
from agentline.config import Config
from agentline.registry import ActionCall, RunRequest

from .conftest import Harness
from .fakes import FakeState, Repos, jira_issue

TICKET_YAML = """
version: 1
name: ticket
nodes:
  read_ticket:
    type: action
    uses: jira.get_ticket
    with: { key: "{{payload.issue_key}}" }
    out: ticket
  note:
    type: action
    uses: jira.comment
    in: [ticket]
    out: comment
    with: { body: "Working on {{ticket.summary}}" }
edges:
  - read_ticket -> note
"""


def test_get_ticket_and_comment(h: Harness, fake: tuple[str, FakeState]) -> None:
    _, state = fake
    state.issues["PROJ-7"] = jira_issue("PROJ-7")
    h.pipeline(TICKET_YAML)
    run = h.submit("ticket", {"issue_key": "PROJ-7"})
    h.drain()
    assert h.status(run) == "done"
    ticket = h.app.result(run, "read_ticket")
    assert ticket["summary"] == "Login broken" and ticket["description"] == "It fails."
    marker = f"[agentline:{run}:note#1]"
    assert h.app.result(run, "note")["marker"] == marker
    (comment,) = state.comments["PROJ-7"]
    text = str(comment["body"])
    assert "Working on Login broken" in text and marker in text
    assert h.effects(run) == {"note#1:comment": "committed"}


def test_comment_posted_once_when_response_is_lost(h: Harness, fake: tuple[str, FakeState]) -> None:
    _, state = fake
    state.issues["PROJ-7"] = jira_issue("PROJ-7")
    state.comments["PROJ-7"] = [  # other comments, so the check has to page
        {"id": str(i), "body": f"old {i}", "author": {}} for i in range(3)
    ]
    state.fail_next_comment_after_write = True  # comment created, then a 503
    h.pipeline(TICKET_YAML)
    run = h.submit("ticket", {"issue_key": "PROJ-7"})
    h.drain()
    assert h.status(run) == "done"
    assert state.comment_creates == 1  # attempt 2's check found attempt 1's marker
    assert h.execution(run, "note#1").attempt == 2


def test_comment_check_after_crash(h: Harness, fake: tuple[str, FakeState]) -> None:
    """A worker died after posting: the intent is left behind; the check finds the marker."""
    _, state = fake
    state.issues["PROJ-7"] = jira_issue("PROJ-7")
    h.pipeline(TICKET_YAML)
    run = h.submit("ticket", {"issue_key": "PROJ-7"})
    state.comments["PROJ-7"] = [
        {"id": "55", "body": f"Working on it\n\n[agentline:{run}:note#1]", "author": {}}
    ]
    h.leave_intent(run, "note", "comment")
    h.drain()
    assert h.status(run) == "done"
    assert state.comment_creates == 0
    assert h.app.result(run, "note")["id"] == "55"


PR_YAML = """
version: 1
name: pr
nodes:
  implement: {{ type: agent, runner: fn, prompt: "go", in: [review?], out: branch }}
  open_pr:
    type: action
    uses: github.open_pr
    in: [branch]
    out: pr
    with: {{ title: "Fix it" {extra} }}
  await_review: {{ type: wait, for: github.review, in: [pr], out: review }}
  route: {{ type: router, switch: review.state }}
  done: {{ type: action, uses: shell.run, with: {{ cmd: "true" }} }}
edges:
  - implement -> open_pr -> await_review -> route
  - {{ from: route, to: implement, when: changes_requested, max_loops: 2 }}
  - {{ from: route, to: done, when: approved }}
"""


def branch_agent(repos: Repos, repo: str = "acme/app") -> Any:
    async def agent(req: RunRequest) -> dict[str, Any]:
        sha = await asyncio.to_thread(repos.commit, f"f{req.iteration}.txt", "x", "feature")
        return {
            "name": "agentline/r1",
            "sha": sha,
            "base_sha": "",
            "repo": repo,
            "mirror": str(repos.mirror),
        }

    return agent


def test_open_pr_pushes_and_reuses_pr_on_iteration_2(
    h: Harness, fake: tuple[str, FakeState], repos: Repos
) -> None:
    _, state = fake
    h.pipeline(PR_YAML.format(extra=f', remote: "{repos.remote}"'), agent=branch_agent(repos))
    run = h.submit("pr")
    h.drain()
    pr1 = h.app.result(run, "open_pr")
    sha1 = h.app.result(run, "implement")["sha"]
    assert pr1["number"] == 1 and pr1["repo"] == "acme/app" and pr1["branch"] == "agentline/r1"
    assert pr1["url"].endswith("/acme/app/pull/1") and pr1["head_sha"] == sha1
    assert repos.remote_sha("agentline/r1") == sha1
    (key,) = [x.correlation_key for x in h.app.executions(run) if x.status == "waiting"]
    assert key == "github:pr:acme/app#1"

    asyncio.run(h.app.deliver_event(key, {"state": "changes_requested", "comments": ["more"]}))
    h.drain()
    sha2 = h.app.result(run, "implement", 2)["sha"]
    pr2 = h.app.result(run, "open_pr", 2)
    assert sha2 != sha1
    assert pr2["number"] == 1 and pr2["head_sha"] == sha2
    assert state.pr_creates == 1  # iteration 2 found the open PR (decision 12)
    assert repos.remote_sha("agentline/r1") == sha2
    effects = h.effects(run)
    assert effects["open_pr#2:push"] == effects["open_pr#2:open_pr"] == "committed"

    asyncio.run(h.app.deliver_event(key, {"state": "approved", "comments": []}))
    h.drain()
    assert h.status(run) == "done"


def test_open_pr_created_once_when_response_is_lost(
    h: Harness, fake: tuple[str, FakeState], repos: Repos
) -> None:
    _, state = fake
    state.fail_next_pr_create_after_write = True  # PR created, then a 502
    h.pipeline(PR_YAML.format(extra=f', remote: "{repos.remote}"'), agent=branch_agent(repos))
    run = h.submit("pr")
    h.drain()
    assert state.pr_creates == 1
    assert h.execution(run, "open_pr#1").attempt == 2
    assert h.app.result(run, "open_pr")["number"] == 1


def test_open_pr_check_after_crash(h: Harness, fake: tuple[str, FakeState], repos: Repos) -> None:
    """Intent left by a dead worker after GitHub created the PR: the check reuses it."""
    _, state = fake
    state.prs["acme/app"] = [
        {
            "number": 9,
            "html_url": "https://github.example/acme/app/pull/9",
            "state": "open",
            "head": {"ref": "agentline/r1", "sha": "x"},
            "base": {"ref": "main"},
        }
    ]
    h.pipeline(PR_YAML.format(extra=", push: false"), agent=branch_agent(repos))
    run = h.submit("pr")
    h.leave_intent(run, "open_pr", "open_pr")
    h.drain()
    assert state.pr_creates == 0
    assert h.app.result(run, "open_pr")["number"] == 9
    assert repos.remote_sha("agentline/r1") is None  # push: false


def test_repo_and_remote_from_config(h: Harness, fake: tuple[str, FakeState], repos: Repos) -> None:
    _, state = fake
    config = Config.from_dict(
        {"repos": {"app": {"remote": str(repos.remote), "github": "acme/app"}}}
    )
    h.pipeline(PR_YAML.format(extra=""), agent=branch_agent(repos, repo="app"), config=config)
    run = h.submit("pr")
    h.drain()
    assert h.app.result(run, "open_pr")["repo"] == "acme/app"
    assert repos.remote_sha("agentline/r1") == h.app.result(run, "implement")["sha"]
    assert state.prs["acme/app"][0]["body"].endswith(f"<!-- agentline run {run} -->")


def test_unknown_repo_fails_permanently(
    h: Harness, fake: tuple[str, FakeState], repos: Repos
) -> None:
    h.pipeline(PR_YAML.format(extra=", push: false"), agent=branch_agent(repos, repo="nope"))
    run = h.submit("pr")
    h.drain()
    ex = h.execution(run, "open_pr#1")
    assert ex.status == "dead" and "repo" in (ex.error or "")


def test_read_reviews_and_waiter_poll(fake: tuple[str, FakeState]) -> None:
    _, state = fake
    from agentline.integrations import GitHubClient

    gh = GitHubClient.from_env()
    gh.create_pull("acme/app", head="feat", base="main", title="T")
    pr = {"repo": "acme/app", "number": 1, "head_sha": "sha2"}
    waiter = ReviewWaiter()
    assert waiter.correlation_key({}, {"pr": pr}) == "github:pr:acme/app#1"
    assert asyncio.run(waiter.poll({}, {"pr": pr})) is None
    state.add_review("acme/app", 1, "changes_requested", commit_id="sha1", body="old")
    state.add_review("acme/app", 1, "commented", commit_id="sha2", body="nit")
    assert asyncio.run(waiter.poll({}, {"pr": pr})) is None  # old commit / not decisive
    state.add_review(
        "acme/app",
        1,
        "approved",
        commit_id="sha2",
        body="LGTM",
        reviewer="bob",
        comments=[{"body": "nice", "path": "a.py", "line": 3}],
    )
    got = asyncio.run(waiter.poll({}, {"pr": pr}))
    assert got is not None
    assert got["state"] == "approved" and got["reviewer"] == "bob"
    assert got["comments"] == ["LGTM", "a.py:3: nice"]
    assert got["source"] == "poll"


def test_open_pr_race_422_reuses_pr(fake: tuple[str, FakeState]) -> None:
    """Someone opens the PR between our lookup and our create: GitHub says 422; reuse it."""
    from agentline.actions.github import OpenPR
    from agentline.integrations import GitHubClient

    _, state = fake

    class RacyClient(GitHubClient):
        def list_pulls(self, repo: str, **kw: Any) -> list[dict[str, Any]]:
            if state.pr_creates == 0:  # first lookup: not there yet; a human creates it now
                GitHubClient.create_pull(self, repo, head="b", base="main", title="human")
                return []
            return super().list_pulls(repo, **kw)

    class Ctx:
        run_id = "r1"

        async def effect(self, name: str, do: Any, **kw: Any) -> Any:
            return await do()

    action = OpenPR(client=lambda: RacyClient.from_env())
    call = ActionCall(params={"repo": "acme/app", "push": False}, inputs={"branch": {"name": "b"}})
    pr = asyncio.run(action(Ctx(), call))  # type: ignore[arg-type]
    assert pr["number"] == 1 and state.pr_creates == 1


def test_merge_pr_merges_once_and_checks_the_head(fake: tuple[str, FakeState]) -> None:
    from agentline.actions.github import MergePR
    from agentline.integrations import GitHubClient

    _, state = fake
    GitHubClient.from_env().create_pull("acme/app", head="feat", base="main", title="T")
    recorded: dict[str, Any] = {}

    class Ctx:
        run_id = "r1"

        async def effect(self, name: str, do: Any, check: Any = None, **kw: Any) -> Any:
            if name in recorded:  # a retry after a crash: the check finds it merged
                return await check()
            recorded[name] = await do()
            return recorded[name]

    action = MergePR()
    moved = ActionCall(
        params={}, inputs={"pr": {"repo": "acme/app", "number": 1, "head_sha": "old"}}
    )
    with pytest.raises(Exception, match="409"):
        asyncio.run(action(Ctx(), moved))  # type: ignore[arg-type]
    call = ActionCall(
        params={}, inputs={"pr": {"repo": "acme/app", "number": 1, "head_sha": "fakesha"}}
    )
    out = asyncio.run(action(Ctx(), call))  # type: ignore[arg-type]
    assert out == {"merged": True, "sha": "merged1", "number": 1, "repo": "acme/app"}
    again = asyncio.run(action(Ctx(), call))  # type: ignore[arg-type]
    assert again["merged"] and state.merges == 1
