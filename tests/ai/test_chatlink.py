"""Chat links over a fake Telegram: pairing, owner only, gates, questions, reports, assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api.editor import PipelineFiles
from agentline.api.server import create_server
from agentline.builtins import default_registry
from agentline.chatlink import state
from agentline.chatlink.service import ChatLink
from agentline.pipeline import build_pipeline, parse_spec
from agentline.registry import ActionCall, RunRequest, RunResult
from agentline.runners.function import FunctionRunner

from .fake_telegram import OWNER, STRANGER, TOKEN, FakeTelegram, fake_telegram

PIPE = """\
version: 1
name: ship
nodes:
  ask:
    type: wait
    for: chat.ask
    with: { question: "Which environment?", choices: [eod, staging] }
    out: answer
  approve: { type: gate, prompt: "Ship to {{answer.text}}?", in: [answer] }
  deploy: { type: action, uses: fake.deploy, in: [answer], out: deployed }
  report:
    type: action
    uses: chat.send
    in: [deployed]
    with: { text: "Shipped to {{deployed.env}}" }
edges:
  - ask -> approve -> deploy -> report
"""
BROKEN = """\
version: 1
name: broken
nodes:
  boom: { type: action, uses: fake.fail, max_attempts: 1 }
"""


@pytest.fixture
def world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ChatLink, FakeTelegram, App]]:
    with fake_telegram() as (base, tg):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
        monkeypatch.setenv("AGENTLINE_TELEGRAM_API", base)
        reg = default_registry()

        @reg.action("fake.deploy", deploy_class=True)
        async def deploy(ctx: Any, call: ActionCall) -> dict[str, Any]:
            return {"env": call.inputs["answer"]["text"]}

        @reg.action("fake.fail")
        async def fail(ctx: Any, call: ActionCall) -> dict[str, Any]:
            raise RuntimeError("disk full")

        async def agent(req: RunRequest) -> RunResult:
            return RunResult(output={"message": "Add OpenRouter?", "choices": ["Yes"],
                                     "actions": [{"type": "key", "name": "OPENROUTER_API_KEY"},
                                                 {"type": "fetch_skills"}]})  # fmt: skip

        reg.add_runner(FunctionRunner("claude-code", agent))
        app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0.0)
        for src in (PIPE, BROKEN):
            app.register_pipeline(build_pipeline(parse_spec(src, base_dir=tmp_path), reg))
        (tmp_path / "pipelines").mkdir()
        link = ChatLink(
            app,
            registry=reg,
            poll_wait=0,
            files=lambda: PipelineFiles(tmp_path / "pipelines", reg, None),
        )
        yield link, tg, app


def step(link: ChatLink) -> None:
    async def go() -> None:
        await link.step(_tg())
        for t in list(link.tasks):
            await t

    asyncio.run(go())


def _tg() -> Any:
    from agentline.chatlink.telegram import Telegram

    return Telegram(TOKEN)


def drain(app: App) -> None:
    asyncio.run(app.worker(concurrency=2).run_until_idle())


def pair(link: ChatLink, tg: FakeTelegram) -> None:
    code = state.new_pair_code()
    tg.text(f"/start {code}")
    step(link)
    assert "Linked to agentline" in tg.last()["text"]


def test_pairing_and_owner_only(world: Any) -> None:
    link, tg, _ = world
    tg.text("hello")
    step(link)
    assert "private agentline bot" in tg.last()["text"]
    state.new_pair_code()
    tg.text("/start 000000")  # wrong code
    step(link)
    assert state.linked() is None
    pair(link, tg)
    assert state.linked()["user_id"] == OWNER
    tg.text("/status", user=STRANGER)
    step(link)
    assert tg.last()["chat_id"] == STRANGER and "only answers its owner" in tg.last()["text"]
    tg.tap("ga:1", user=STRANGER)
    step(link)  # ignored, answered "Not allowed"
    tg.text("/runs")
    step(link)
    assert tg.last()["text"] == "No runs yet."


def test_question_gate_with_deploy_confirmation_and_report(world: Any) -> None:
    link, tg, app = world
    pair(link, tg)
    sub = app.submit_sync("ship", {})
    drain(app)
    step(link)  # sends the question
    q = tg.last()
    assert "Which environment?" in q["text"]
    assert tg.buttons(q) == [("eod", "q:1:0"), ("staging", "q:1:1")]
    tg.text("staging", reply_to=q["message_id"])
    step(link)
    assert tg.last()["text"] == "Thanks, passed it on."
    drain(app)
    step(link)  # the gate
    g = tg.last()
    assert "Ship to staging?" in g["text"] and "guards a deploy" in g["text"]
    tg.tap("ga:1")
    step(link)
    assert "This approves a deploy" in tg.last()["text"]  # needs a second tap
    assert app.get_run(sub.run_id).status == "waiting"
    tg.tap("gc:1")
    step(link)
    assert tg.last()["text"] == "Approved approve of run " + sub.run_id + "."
    drain(app)
    assert app.get_run(sub.run_id).status == "done"
    assert any(m["text"] == "Shipped to staging" for m in tg.sent)  # chat.send
    with app.ledger.tx() as tx:
        audit = [a for a in tx.list_audit(sub.run_id) if a.action == "approve"]
    assert audit and audit[0].detail["via"] == "telegram:@user42"


def test_failures_are_reported_once(world: Any) -> None:
    link, tg, app = world
    pair(link, tg)
    app.submit_sync("broken", {})
    drain(app)
    step(link)
    assert tg.last()["text"].startswith("❌ broken run") and "disk full" in tg.last()["text"]
    assert tg.buttons(tg.last()) == [
        ("Retry boom", "rt:1"), ("Fix it", "fx:1"), ("👍", "fu:1"), ("👎", "fd:1"),
    ]  # fmt: skip
    n = len(tg.sent)
    step(link)
    assert len(tg.sent) == n
    tg.tap("rt:1")
    step(link)
    assert tg.last()["text"].startswith("Retrying run") and "at boom" in tg.last()["text"]
    tg.tap("fx:1")  # the assistant gets the failure (the fake agent answers "Add OpenRouter?")
    step(link)
    assert tg.last()["text"].startswith("Add OpenRouter?")


def test_free_text_goes_to_the_assistant(world: Any) -> None:
    link, tg, _ = world
    pair(link, tg)
    tg.text("set me up")
    step(link)
    reply = tg.last()
    assert reply["text"].startswith("Add OpenRouter?")
    assert "keys never go through chat" in reply["text"]
    assert tg.buttons(reply) == [("Apply: download the skill library", "p:1"), ("Yes", "s:0")]
    assert state.read()["history"][-1] == {"role": "assistant", "content": "Add OpenRouter?"}


def test_chat_api(world: Any, tmp_path: Path) -> None:
    link, tg, app = world
    (tmp_path / "p2").mkdir()
    c = TestClient(create_server(app, [], pipelines_dir=tmp_path / "p2"))
    v = c.get("/api/chat").json()
    assert v["telegram"]["token_set"] and v["telegram"]["bot"] == "agentline_test_bot"
    assert v["telegram"]["linked"] is None and v["notify"]["gates"]
    r = c.post("/api/chat/telegram/pair").json()
    assert r["link"] == f"https://t.me/agentline_test_bot?start={r['code']}"
    assert c.post("/api/chat/telegram/test").status_code == 409
    tg.text(f"/start {r['code']}")
    step(link)
    assert c.get("/api/chat").json()["telegram"]["linked"]["user_id"] == OWNER
    assert c.post("/api/chat/telegram/test").json() == {"sent": True}
    assert c.put("/api/chat/notify", json={"done": True}).json()["notify"]["done"] is True
    assert c.delete("/api/chat/telegram").json()["telegram"]["linked"] is None
    assert "TELEGRAM_BOT_TOKEN" in {k["name"] for k in c.get("/api/settings").json()["keys"]}


def test_state_stays_bounded() -> None:
    with state.edit() as d:
        d["sent"] = {f"x:{i}": i for i in range(1500)}
        d["history"] = [{"role": "user", "content": str(i)} for i in range(100)]
    d = state.read()
    assert len(d["sent"]) == 1000 and "x:1499" in d["sent"] and "x:0" not in d["sent"]
    assert len(d["history"]) == 40


def test_budget_alert_goes_to_the_chat(world: Any) -> None:
    from agentline import costs

    link, tg, app = world
    pair(link, tg)
    costs.set_budget(0.000001)
    with app.ledger.tx() as tx:  # some spend this month
        from agentline.ledger.base import Run

        tx.insert_run(Run(id="rc", flow="ship", flow_hash="h", payload={}, status="done",
                          created_at=__import__("time").time()))  # fmt: skip
        tx.add_cost("rc", "x1", 1, 0.5, "runner:fn")
    step(link)
    alerts = [m["text"] for m in tg.sent if m["text"].startswith("💸")]
    assert len(alerts) == 1 and "100%" in alerts[0]


def test_triage_verdicts_go_to_the_chat_once(world: Any) -> None:
    import time

    from agentline.triage import TriageStore

    link, tg, _ = world
    pair(link, tg)
    store = TriageStore()
    rec = {"id": "t0123456789ab", "run": "r1", "flow": "broken", "node": "boom",
           "status": "done", "verdict": "external", "summary": "The disk is full.",
           "actions": [{"type": "retry", "run": "r1", "node": "boom", "issues": []}],
           "created_at": time.time()}  # fmt: skip
    store.save(rec)
    store.save({**rec, "id": "t0123456789ac", "status": "running", "verdict": None})
    step(link)
    texts = [m["text"] for m in tg.sent if m["text"].startswith("🔎")]
    assert texts == [
        "🔎 Triage of broken run r1 at boom: external\nThe disk is full.\n"
        "Proposed: retry run r1 at boom. Apply them in Agentline Studio."
    ]
    step(link)
    assert len([m for m in tg.sent if m["text"].startswith("🔎")]) == 1


HELPDESK = """\
version: 1
name: helpdesk
nodes:
  intake:
    type: chat
    prompt: "Collect the repo."
    output_schema: { type: object, required: [repo], properties: { repo: { type: string } } }
    out: request
  work: { type: action, uses: fake.fail, in: [request], max_attempts: 1 }
edges:
  - intake -> work
"""


def test_gatekeeper_talks_in_the_chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A chat start node over Telegram: /run with words, replies reach the run's thread, its
    messages and the pipeline's notes come back, a card's button confirms a retry."""
    import json

    from agentline import runchat

    def gatekeeper(req: RunRequest) -> RunResult:
        if "it is running now" in req.prompt:
            return RunResult(output=json.dumps({"say": "work failed. Retry?", "action": "none",
                             "run_actions": [{"type": "retry", "node": "work"}]}))  # fmt: skip
        if "Person: acme/web" in req.prompt:
            return RunResult(output={"say": "Starting.", "action": "handoff",
                                     "output": {"repo": "acme/web"}})  # fmt: skip
        return RunResult(output={"say": "Which repo?", "action": "ask"})

    with fake_telegram() as (base, tg):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
        monkeypatch.setenv("AGENTLINE_TELEGRAM_API", base)
        reg = default_registry()

        @reg.action("fake.fail")
        async def fail(ctx: Any, call: ActionCall) -> dict[str, Any]:
            raise RuntimeError("disk full")

        async def agent(req: RunRequest) -> RunResult:
            return gatekeeper(req)

        reg.add_runner(FunctionRunner("claude-code", agent))
        app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0.0)
        app.register_pipeline(build_pipeline(parse_spec(HELPDESK, base_dir=tmp_path), reg))
        (tmp_path / "pipelines").mkdir()
        files = PipelineFiles(tmp_path / "pipelines", reg, None)
        link = ChatLink(app, registry=reg, poll_wait=0, files=lambda: files)
        pair(link, tg)
        tg.text("/run helpdesk")
        step(link)
        assert "starts from chat" in tg.last()["text"]
        tg.text("/run helpdesk fix the login bug")
        step(link)
        assert "Its gatekeeper answers here" in tg.last()["text"]
        run = app.get_run(tg.last()["text"].split("run ")[1].split(".")[0])
        assert run is not None and run.payload == {"text": "fix the login bug"}
        drain(app)
        step(link)
        ask = tg.last()
        assert "· intake:\n\nWhich repo?" in ask["text"] and "Reply to this" in ask["text"]
        tg.text("acme/web", reply_to=ask["message_id"])
        step(link)
        drain(app)
        assert app.result(run.id, "intake") == {"repo": "acme/web"}
        assert app.get_run(run.id).status == "dead"
        asyncio.run(runchat.RunChatLoop(app, files).step())
        before = len(tg.sent)
        step(link)
        texts = [m["text"] for m in tg.sent[before:]]
        assert any("intake:\n\nStarting." in t for t in texts)
        assert any("pipeline:\n\nwork failed: RuntimeError: disk full" in t for t in texts), texts
        assert not any(t.startswith("❌") for t in texts)  # no generic notice: the thread has it
        card = tg.sent[-1]
        assert tg.buttons(card) == [("Confirm: Retry work", "rc:1"), ("Dismiss", "rd:1")], texts
        tg.tap("rc:1")
        step(link)
        assert [x.status for x in app.executions(run.id) if x.node_id == "work"] == ["ready"]
        step(link)
        assert tg.last()["text"].endswith("retry work: done")
    with app.ledger.tx() as tx:
        audit = [a for a in tx.list_audit(run.id) if a.action == "retry"]
    assert audit[0].detail["via"] == "telegram:@user42"
    assert audit[0].actor == "telegram:@user42"  # the card's tap, not the OS user (spec 011)


def test_rate_a_run_from_the_chat(world: Any) -> None:
    from agentline import feedback

    link, tg, app = world
    pair(link, tg)
    sub = app.submit_sync("broken", {})
    drain(app)
    step(link)
    tg.tap("fd:1")  # 👎 under the run's end notice
    step(link)
    assert "rated 👎" in tg.last()["text"] and "What went wrong?" in tg.last()["text"]
    tg.text(f"/rate {sub.run_id} down the disk was full again")
    step(link)
    assert "with your comment" in tg.last()["text"]
    tg.text("/rate nope up")
    step(link)
    assert tg.last()["text"] == "No run nope."
    tg.text(f"/rate {sub.run_id} meh")
    step(link)
    assert "rating must be" in tg.last()["text"]
    rows = feedback.entries(app, sub.run_id)
    assert [r["rating"] for r in rows] == ["down", "down"]
    assert rows[-1]["comment"] == "the disk was full again"
    assert rows[0]["by"] == f"telegram:@user{state.linked()['user_id']}"
    assert sub.run_id in feedback.read_pending()  # the review loop will look at it


# -- who chat-link actions are recorded as (spec 011) ------------------------------------------


def _gated(link: ChatLink, app: App, tmp_path: Path, name: str, approvers: str = "") -> None:
    extra = f", approvers: [{approvers}]" if approvers else ""
    src = (f"version: 1\nname: {name}\nnodes:\n"
           f"  ok: {{ type: gate, prompt: \"Go?\"{extra} }}\n")  # fmt: skip
    app.register_pipeline(build_pipeline(parse_spec(src, base_dir=tmp_path), link.registry))


def _actors(app: App, run_id: str, action: str) -> list[str]:
    with app.ledger.tx() as tx:
        return [a.actor for a in tx.list_audit(run_id) if a.action == action]


def _decide(link: ChatLink, tg: FakeTelegram, app: App, name: str, verb: str = "approve") -> str:
    run_id = app.submit_sync(name, {}).run_id
    drain(app)
    tg.text(f"/{verb} {run_id}")
    step(link)
    return run_id


def test_chat_actions_name_the_chat_user_not_the_os_user(
    world: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link, tg, app = world
    monkeypatch.setenv("AGENTLINE_USER", "osuser")
    _gated(link, app, tmp_path, "g")
    pair(link, tg)  # a code made by the local user: no person
    assert "person" not in state.linked()
    a = _decide(link, tg, app, "g")
    assert _actors(app, a, "approve") == ["telegram:@user42"]
    r = _decide(link, tg, app, "g", "reject")
    assert _actors(app, r, "reject") == ["telegram:@user42"]
    broken = app.submit_sync("broken", {}).run_id
    drain(app)
    tg.text(f"/retry {broken}")
    step(link)
    assert _actors(app, broken, "retry") == ["telegram:@user42"]


def test_a_persons_pairing_code_links_a_chat_that_acts_as_them(
    world: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentline.people import PeopleStore

    link, tg, app = world
    monkeypatch.setenv("AGENTLINE_USER", "osuser")
    PeopleStore().add("alice")
    _gated(link, app, tmp_path, "g")
    tg.text(f"/start {state.new_pair_code('alice')}")
    step(link)
    assert state.linked()["person"] == "alice"
    a = _decide(link, tg, app, "g")
    assert _actors(app, a, "approve") == ["alice"]
    with app.ledger.tx() as tx:
        row = next(x for x in tx.list_audit(a) if x.action == "approve")
    assert row.detail["via"] == "telegram:@user42"  # still says which chat it came through
    PeopleStore().revoke("alice")  # removed later: the chat user, not the removed name
    b = _decide(link, tg, app, "g")
    assert _actors(app, b, "approve") == ["telegram:@user42"]


def test_an_old_link_without_a_username_acts_as_the_user_id(
    world: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link, tg, app = world
    monkeypatch.setenv("AGENTLINE_USER", "osuser")
    _gated(link, app, tmp_path, "g")
    with state.edit() as d:  # paired before spec 011: no person, and here no username either
        d["telegram"] = {"chat_id": OWNER, "user_id": OWNER, "username": "", "linked_at": 1.0}
    a = _decide(link, tg, app, "g")
    assert _actors(app, a, "approve") == [f"telegram:@{OWNER}"]


def test_gate_approvers_are_checked_against_the_chat_actor(
    world: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentline.people import PeopleStore

    link, tg, app = world
    monkeypatch.setenv("AGENTLINE_USER", "osuser")
    PeopleStore().add("alice")
    _gated(link, app, tmp_path, "for_alice", "alice")
    _gated(link, app, tmp_path, "for_os", "osuser")
    tg.text(f"/start {state.new_pair_code('alice')}")
    step(link)
    a = _decide(link, tg, app, "for_alice")
    assert app.get_run(a).status == "done"
    with state.edit() as d:  # the same chat, linked by nobody in particular
        d["telegram"].pop("person")
    before = len(tg.sent)
    b = _decide(link, tg, app, "for_os")
    assert any("not an approver" in m["text"] for m in tg.sent[before:])
    assert app.get_run(b).status == "waiting" and _actors(app, b, "approve") == []


def test_chat_api_pair_records_the_person_and_shows_acts_as(
    world: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentline.people import PeopleStore

    link, tg, app = world
    monkeypatch.setenv("AGENTLINE_USER", "osuser")
    monkeypatch.delenv("AGENTLINE_API_TOKEN", raising=False)
    _, token = PeopleStore().add("alice")
    (tmp_path / "p3").mkdir()
    c = TestClient(create_server(app, [], pipelines_dir=tmp_path / "p3"))
    c.post("/api/chat/telegram/pair")  # the local user: no person
    assert "person" not in state.read()["pair"]
    r = c.post("/api/chat/telegram/pair", headers={"Authorization": f"Bearer {token}"}).json()
    assert state.read()["pair"]["person"] == "alice"
    assert c.get("/api/chat").json()["telegram"]["acts_as"] is None
    tg.text(f"/start {r['code']}")
    step(link)
    assert c.get("/api/chat").json()["telegram"]["acts_as"] == "alice"
    PeopleStore().revoke("alice")
    assert c.get("/api/chat").json()["telegram"]["acts_as"] == "telegram:@user42"
