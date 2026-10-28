"""The chat start node (gatekeeper): intake turns, resume-on-event, the run loop's notes and
answers, cards, and the start point (agentline.runchat, agentline.pipeline.start)."""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentline import App, SqliteLedger, runchat
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.pipeline import build_pipeline, parse_spec, validate_spec
from agentline.pipeline.start import start_info

from .conftest import OPENROUTER_TOML
from .fakes import KEY, FakeLLM, fake_llm

PIPELINE = """\
name: helpdesk
defaults: { harness: builtin, model: cheap }
nodes:
  intake:
    type: chat
    prompt: "Collect the repo and the task."
    output_schema:
      type: object
      required: [repo, task]
      properties: { repo: { type: string }, task: { type: string } }
    out: request
  work:
    type: action
    uses: shell.run
    in: [request]
    max_attempts: 1
    with: { cmd: "%s", permanent: true }
  ship:
    type: gate
    in: [request]
    prompt: "Ship {{request.task}}?"
edges:
  - intake -> work -> ship
"""


def build(base: str, tmp_path: Path, cmd: str = "true") -> tuple[App, Any, Any]:
    cfg = Config.from_dict(tomllib.loads(OPENROUTER_TOML.format(base=base)))
    spec = parse_spec(PIPELINE % cmd)
    reg = default_registry(cfg)
    app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0)
    app.register_pipeline(build_pipeline(spec, reg, cfg))
    return app, spec, SimpleNamespace(registry=reg, config=cfg)


def j(**kw: Any) -> str:
    return json.dumps(kw)


def idle(app: App) -> None:
    asyncio.run(app.worker(concurrency=1).run_until_idle())


def roles(app: App, run_id: str) -> list[str]:
    return [m.role for m in runchat.thread(app, run_id)]


def status(app: App, run_id: str, node: str) -> str:
    return [x for x in app.executions(run_id) if x.node_id == node][-1].status


def test_intake_asks_then_hands_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    replies = [
        j(say="Which repo?", action="ask"),
        j(say="On it.", action="handoff", output={"repo": "acme/web", "task": "fix login"}),
    ]
    with fake_llm(FakeLLM(replies=replies)) as (base, st):
        app, _, _ = build(base, tmp_path)
        run = app.submit_sync("helpdesk", {"text": "fix the login bug"}).run_id
        idle(app)
        assert status(app, run, "intake") == "waiting"
        assert roles(app, run) == ["user", "agent"]
        assert app.get_run(run).status == "waiting"
        asyncio.run(runchat.say(app, run, "acme/web"))
        idle(app)
    assert app.result(run, "intake") == {"repo": "acme/web", "task": "fix login"}
    assert status(app, run, "work") == "done" and status(app, run, "ship") == "waiting"
    assert roles(app, run) == ["user", "agent", "user", "agent"]
    second = st.requests[1]["messages"][-1]["content"]
    assert "Person: fix the login bug\nYou: Which repo?\nPerson: acme/web" in second
    assert "Collect the repo and the task." in second and '"required"' in second
    # the chat wait is not a gate: approvals only list `ship`
    assert [x.node_id for x in app.waiting_gates(run)] == ["ship"]


def test_messages_while_a_turn_runs_wake_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    with fake_llm(FakeLLM(replies=[j(say="Which repo?", action="ask")])) as (base, st):
        app, _, _ = build(base, tmp_path)
        run = app.submit_sync("helpdesk", {"text": "fix the login bug"}).run_id
        # two messages arrive before the first turn: one turn reads them all
        asyncio.run(runchat.say(app, run, "it's urgent"))
        asyncio.run(runchat.say(app, run, "really"))
        idle(app)
    assert len(st.requests) == 1  # the extra wake had nothing new to answer
    assert "Person: really" in st.requests[0]["messages"][-1]["content"]
    assert status(app, run, "intake") == "waiting"
    assert roles(app, run) == ["user", "user", "user", "agent"]


def test_bad_reply_is_repaired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    replies = [
        j(say="done", action="handoff", output={"repo": "acme/web"}),  # misses `task`
        j(say="done", action="handoff", output={"repo": "acme/web", "task": "x"}),
    ]
    with fake_llm(FakeLLM(replies=replies)) as (base, st):
        app, _, _ = build(base, tmp_path)
        run = app.submit_sync("helpdesk", {"text": "x in acme/web"}).run_id
        idle(app)
    assert "missing required property 'task'" in st.requests[1]["messages"][-1]["content"]
    assert app.result(run, "intake")["task"] == "x"


def test_loop_reports_failure_and_retry_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    replies = [
        j(say="Starting.", action="handoff", output={"repo": "a/b", "task": "t"}),
        j(say="work failed: exit 3. Retry?", action="none",
          run_actions=[{"type": "retry", "node": "work", "comment": "flaky"}]),
    ]  # fmt: skip
    with fake_llm(FakeLLM(replies=replies)) as (base, st):
        app, _, files = build(base, tmp_path, cmd="exit 3")
        run = app.submit_sync("helpdesk", {"text": "t in a/b"}).run_id
        idle(app)
        assert app.get_run(run).status == "dead"
        loop = runchat.RunChatLoop(app, files)
        asyncio.run(loop.step())
        msgs = runchat.thread(app, run)
        assert [m.role for m in msgs] == ["user", "agent", "system", "system", "agent"]
        assert msgs[2].text.startswith("work failed") and msgs[3].text == "The run is dead."
        assert "work (action, iteration 1): dead" in st.requests[1]["messages"][-1]["content"]
        card = msgs[4].detail["actions"][0]
        assert card == {"type": "retry", "node": "work", "comment": "flaky", "state": "proposed"}
        asyncio.run(loop.step())  # nothing new: no second answer
        assert len(st.requests) == 2
        done = asyncio.run(runchat.apply_card(app, run, msgs[4].id, 0, actor="me"))
        assert done["state"] == "done"
        assert runchat.thread(app, run)[-1].text == "retry work: done"
        assert status(app, run, "work") == "ready"
        with pytest.raises(Exception, match="already done"):
            asyncio.run(runchat.apply_card(app, run, msgs[4].id, 0, actor="me"))
    audit = [a for a in app.audit_log(run) if a.action == "retry"]
    assert audit and audit[0].detail["via"] == "run-chat"


def test_cancel_from_intake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    with fake_llm(FakeLLM(replies=[j(say="Stopping.", action="cancel")])) as (base, _):
        app, _, files = build(base, tmp_path)
        run = app.submit_sync("helpdesk", {"text": "never mind"}).run_id
        idle(app)
        asyncio.run(runchat.RunChatLoop(app, files).step())
    assert app.get_run(run).status == "cancelled"


def test_say_needs_a_chat_pipeline(tmp_path: Path) -> None:
    app = App(SqliteLedger(tmp_path / "l.db"))
    with pytest.raises(Exception, match="no run"):
        asyncio.run(runchat.say(app, "nope", "hi"))


# -- start point ------------------------------------------------------------------------------
def test_start_info_template_and_text_input() -> None:
    spec = parse_spec(
        """\
name: p
nodes:
  build: { type: action, uses: shell.run, with: { cmd: "true" } }
  plan: { type: agent, prompt: "Do {{payload.task}} in {{payload.repo.name}}" }
edges: [build -> plan]
"""
    )
    assert start_info(spec) == {
        "node": "build",
        "input": "json",
        "template": {"repo": {"name": ""}, "task": ""},
        "fields": ["repo.name", "task"],
    }
    spec = parse_spec(
        """\
name: p
start: { node: plan, input: text }
nodes:
  plan: { type: agent, prompt: "Plan it." }
"""
    )
    assert start_info(spec)["input"] == "text" and start_info(spec)["template"] is None


def test_text_input_goes_to_the_start_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    src = """\
name: plain
start: { input: text }
defaults: { harness: builtin, model: cheap }
nodes:
  plan: { type: agent, prompt: "Plan the work." }
"""
    with fake_llm(FakeLLM(replies=["ok"])) as (base, st):
        cfg = Config.from_dict(tomllib.loads(OPENROUTER_TOML.format(base=base)))
        app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0)
        reg = default_registry(cfg)
        app.register_pipeline(build_pipeline(parse_spec(src), reg, cfg))
        app.submit_sync("plain", {"text": "add dark mode"})
        idle(app)
    assert (
        st.requests[0]["messages"][-1]["content"] == "Plan the work.\n\n## Input\nadd dark mode\n"
    )


def test_start_point_validation() -> None:
    def issues(src: str) -> list[str]:
        return [f"{i.path}: {i.message}" for i in validate_spec(parse_spec(src))]

    base = "name: p\nnodes:\n  a: { type: agent, prompt: hi }\n  b: { type: agent, prompt: hi }\n"
    assert issues(base + "start: { node: b }\nedges: [a -> b]\n") == [
        "start.node: 'b' has an incoming edge, so it can't take the input"
    ]
    assert issues(base + "start: { node: zz }\nedges: [a -> b]\n") == [
        "start.node: unknown node 'zz'"
    ]
    assert issues(base + "start: { input: text, template: {x: 1} }\nedges: [a -> b]\n") == [
        "start.template: template only applies to json input"
    ]
    chat = "name: p\nnodes:\n  c: { type: chat, prompt: hi, allowed_tools: [read, bash] }\n"
    found = issues(chat + "  d: { type: agent, prompt: hi }\n")
    assert "nodes.c: a chat node must be the pipeline's only start node" in found
    assert any("no bash tools" in i for i in found)
    assert issues(chat.replace(", bash", "") + "start: { input: text }\n") == [
        "start.input: a chat start node takes chat input"
    ]


# -- API ----------------------------------------------------------------------------------------
def test_api_start_converse_and_cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from agentline.api.server import create_server

    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    replies = [
        j(say="Which repo?", action="ask"),
        j(say="Starting.", action="handoff", output={"repo": "a/b", "task": "t"}),
        j(
            say="work failed. Retry?",
            action="none",
            run_actions=[{"type": "retry", "node": "work"}],
        ),
    ]
    with fake_llm(FakeLLM(replies=replies)) as (base, _):
        app, _, files = build(base, tmp_path, cmd="exit 3")
        (tmp_path / "p").mkdir()
        with TestClient(create_server(app, [], pipelines_dir=tmp_path / "p", token="")) as c:
            info = c.get("/api/runs/start-info", params={"pipeline": "helpdesk"}).json()
            assert info == {"node": "intake", "input": "chat", "template": None, "fields": []}
            assert c.get("/api/runs/start-info", params={"pipeline": "x"}).status_code == 404
            run = c.post("/api/runs", json={"pipeline": "helpdesk", "text": "fix t"}).json()
            rid = run["run_id"]
            assert c.get(f"/api/runs/{rid}/messages").json()["thinking"] is True  # ready
            idle(app)
            got = c.get(f"/api/runs/{rid}/messages").json()
            assert (got["messages"][0]["id"], got["messages"][0]["role"]) == (0, "user")
            assert got["messages"][1]["text"] == "Which repo?" and got["thinking"] is False
            last = got["messages"][-1]["id"]
            assert c.post(f"/api/runs/{rid}/messages", json={"text": " "}).status_code == 409
            assert c.post(f"/api/runs/{rid}/messages", json={"text": "a/b"}).status_code == 200
            idle(app)
            new = c.get(f"/api/runs/{rid}/messages", params={"after": last}).json()["messages"]
            assert [m["text"] for m in new] == ["a/b", "Starting."]
            asyncio.run(runchat.RunChatLoop(app, files).step())
            msgs = c.get(f"/api/runs/{rid}/messages").json()["messages"]
            card_msg = msgs[-1]
            assert card_msg["detail"]["actions"][0]["state"] == "proposed"
            url = f"/api/runs/{rid}/messages/{card_msg['id']}/apply"
            r = c.post(url, json={"index": 0, "dismiss": True})
            assert r.json()["card"]["state"] == "dismissed"
            assert c.post(url, json={"index": 0}).status_code == 409


def test_dry_run_hands_off_at_once(tmp_path: Path) -> None:
    from agentline.dryrun import dry_run

    spec = parse_spec(PIPELINE % "true")
    cfg = Config.from_dict(tomllib.loads(OPENROUTER_TOML.format(base="http://x.test/v1")))
    out = asyncio.run(dry_run(spec, default_registry(cfg), cfg, payload={"text": "hi"}))
    assert out["status"] in ("waiting", "done"), out
