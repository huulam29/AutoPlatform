"""People's feedback, run metrics, run reports and insights (phase 58), and how feedback
reaches run reviews."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import feedback, metrics, review, runreport
from agentline.api.server import create_server
from agentline.cli import main
from agentline.errors import ValidationError

from .test_review import World, reviewer_reply

FAKE_SECRET = "sk-fake-FEEDBACK-0123456789"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("FAKE_API_KEY", FAKE_SECRET)
    w = World(tmp_path)
    w.app.redactor.add("FAKE_API_KEY", FAKE_SECRET)
    return w


# -- feedback --------------------------------------------------------------------------------
def test_ratings_are_recorded_redacted_and_newest_wins(world: World) -> None:
    run = world.run(loops=0)
    e = feedback.add(world.app, run, "down", f"it leaked {FAKE_SECRET}", by="ana")
    assert FAKE_SECRET not in e["comment"] and "it leaked" in e["comment"]
    feedback.add(world.app, run, "up", by="ana")  # ana changes her mind
    feedback.add(world.app, run, "down", by="bo")
    feedback.add(world.app, run, "up", "good plan", node="write", by="ana")
    rows = feedback.entries(world.app, run)
    assert len(rows) == 4 and all(FAKE_SECRET not in json.dumps(r) for r in rows)
    s = feedback.summarize(rows)
    assert (s["up"], s["down"], s["comments"]) == (2, 1, 2)
    assert s["run"] == {"up": 1, "down": 1}
    assert s["nodes"] == {"write": {"up": 1, "down": 0}}
    feedback.add(world.app, run, "none", by="bo")  # withdraw
    assert feedback.summary(world.app, run)["down"] == 0
    with world.app.ledger.tx() as tx:  # it is in the journal; runs.updated_at is untouched
        j = [x for x in tx.list_journal(run) if x.kind == "feedback"]
        assert j[0].message.startswith("feedback on the run: 👎")
        assert j[3].node_id == "write" and j[3].iteration == 1


def test_bad_feedback_is_refused(world: World) -> None:
    run = world.run(loops=0)
    with pytest.raises(ValidationError, match="rating must be"):
        feedback.add(world.app, run, "meh")
    with pytest.raises(ValidationError, match="at most 2000"):
        feedback.add(world.app, run, "down", "x" * 2001)
    with pytest.raises(ValidationError, match="no node"):
        feedback.add(world.app, run, "down", node="ghost")
    with pytest.raises(ValidationError, match="no iteration 9"):
        feedback.add(world.app, run, "down", node="write", iteration=9)
    with pytest.raises(KeyError):
        feedback.add(world.app, "r-none", "up")


def test_only_negative_feedback_is_pending_for_review(world: World) -> None:
    run = world.run(loops=0)
    feedback.add(world.app, run, "up")
    assert run not in feedback.read_pending()
    e = feedback.add(world.app, run, "up", "nice, but name the ticket")  # a comment counts
    assert feedback.read_pending()[run]["id"] == e["id"]
    feedback.clear_pending(run, e["id"] - 1)  # an older review doesn't clear newer feedback
    assert run in feedback.read_pending()
    feedback.clear_pending(run, e["id"])
    assert run not in feedback.read_pending()


# -- metrics ---------------------------------------------------------------------------------
def test_run_metrics_from_journal_and_runner_meta(world: World) -> None:
    run = world.run(loops=1)
    with world.app.ledger.tx() as tx:  # a runner that reported usage and turns
        x = next(x for x in tx.list_executions(run) if x.node_id == "write")
        detail = {**x.detail, "runner_meta.first": {"usage": {"input_tokens": 100,
                  "output_tokens": 20, "cache_read_input_tokens": 5}, "turns": 3,
                  "model": "claude-x"}}  # fmt: skip
        tx.update_execution(x.id, detail=detail)
    feedback.add(world.app, run, "down", node="write", by="ana")
    m = metrics.run_metrics(world.app, run)
    assert m is not None and m["finished"] and m["status"] == "done"
    nodes = {n["node"]: n for n in m["nodes"]}
    assert set(nodes) == {"write", "check", "r", "ship"}
    w = nodes["write"]
    assert w["executions"] == 2 and w["iterations"] == 2 and w["kind"] == "agent"
    assert w["tokens"] == {"input": 100, "output": 20, "cache_read": 5, "cache_write": 0}
    assert w["turns"] == 3 and w["models"] == ["claude-x"]
    assert w["feedback"] == {"up": 0, "down": 1}
    assert nodes["ship"]["tokens"] is None and nodes["ship"]["turns"] is None  # unknown, not 0
    assert m["totals"]["tokens"]["input"] == 100
    assert m["node_cost_usd"] == pytest.approx(0.02)  # two drafts at $0.01
    assert m["totals"]["executions"] == sum(n["executions"] for n in m["nodes"])
    assert m["feedback"]["down"] == 1


def test_waiting_time_is_not_work() -> None:
    class J:
        def __init__(self, at: float, message: str) -> None:
            self.at, self.message, self.id, self.kind = at, message, int(at), "status"

    steps = [
        (0, "created (ready)"),
        (2, "claimed -> running"),
        (3, "running -> waiting"),
        (603, "waiting -> ready"),
        (604, "ready -> running"),
        (605, "running -> done"),
    ]
    rows = [J(at, msg) for at, msg in steps]
    t = metrics._times(rows, end=999)
    assert t == {"queued_s": 3.0, "work_s": 2.0, "waiting_s": 600.0}


def test_insights_per_pipeline_day_and_node(world: World) -> None:
    a = world.run(loops=0)
    b = world.run(loops=2)
    feedback.add(world.app, b, "down", "two loops again", node="check")
    rep = metrics.insights(world.app, days=7)
    [p] = rep["pipelines"]
    assert p["pipeline"] == "p" and p["runs"] == 2 and p["done"] == 2
    assert p["success_rate"] == 1.0 and p["loops"] == 2  # run b took the loop twice
    assert p["down"] == 1 and len(rep["by_day"]) == 7 and rep["by_day"][-1]["runs"] == 2
    assert rep["nodes"] == []  # only with a pipeline
    rep = metrics.insights(world.app, days=7, pipeline="p")
    assert rep["nodes"][0]["node"] == "check" and rep["nodes"][0]["down"] == 1
    assert metrics.insights(world.app, days=7, pipeline="other")["pipelines"] == []
    assert a


# -- reports ---------------------------------------------------------------------------------
def test_report_is_redacted_and_capped(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    run = world.run(loops=0)
    feedback.add(world.app, run, "down", f"see {FAKE_SECRET}")
    with world.app.ledger.tx() as tx:
        tx.update_run(run, error=f"boom with {FAKE_SECRET}")
        for i in range(30):
            tx.add_journal(run, "note", f"note {i}")
    monkeypatch.setattr(runreport, "MAX_TIMELINE", 10)
    rep = runreport.build(world.app, run, triage_store=world.tstore, review_store=world.store)
    assert rep is not None
    text = json.dumps(rep, default=str)
    md = runreport.markdown(rep)
    assert FAKE_SECRET not in text and FAKE_SECRET not in md
    assert len(rep["timeline"]) == 10 and rep["timeline_omitted"] > 20
    assert "more line(s) left out" in md and "## Nodes" in md and "| write |" in md
    assert rep["feedback"]["summary"]["down"] == 1
    assert {o["node"] for o in rep["outputs"]} == {"write", "check", "r", "ship"}


# -- reviews read the feedback ---------------------------------------------------------------
def test_feedback_triggers_one_review_with_the_comment(world: World) -> None:
    run = world.run(loops=0)  # a clean run: no signals, never reviewed on its own
    since = time.time() - 60
    later = time.time() + 60
    assert review.find_runs(world.app, world.store, since, world.cfg, now=later,
                            triage_store=world.tstore) == []  # fmt: skip
    feedback.add(world.app, run, "up")  # thumbs up alone triggers nothing
    assert review.find_runs(world.app, world.store, since, world.cfg, now=later,
                            triage_store=world.tstore) == []  # fmt: skip
    e = feedback.add(world.app, run, "down", "the draft ignored the acceptance criteria")
    soon = e["at"] + 1  # within the settle window: wait for more feedback
    assert review.find_runs(world.app, world.store, since, world.cfg, now=soon,
                            triage_store=world.tstore) == []  # fmt: skip
    found = review.find_runs(world.app, world.store, since, world.cfg, now=later,
                             triage_store=world.tstore)  # fmt: skip
    assert [s["run"] for s in found] == [run]
    sig = found[0]
    assert sig["feedback"]["down"] == 1 and sig["trouble"] >= 1
    assert any("people's feedback" in line for line in review.signal_lines(sig))
    world.replies.append(reviewer_reply())
    rec = world.review(run, sig=sig, force=bool(sig.pop("force", False)))
    assert rec["status"] == "done" and rec["feedback_upto"] == e["id"]
    prompt = world.asked[-1].prompt
    assert "PEOPLE'S FEEDBACK" in prompt and "ignored the acceptance criteria" in prompt
    assert run not in feedback.read_pending()
    assert review.find_runs(world.app, world.store, since, world.cfg, now=later,
                            triage_store=world.tstore) == []  # fmt: skip
    # new feedback after the review: reviewed again (keeping what was adopted)
    rec["adopted"] = {"0": "a0123456789ab"}
    world.store.save(rec)
    e2 = feedback.add(world.app, run, "down", "and no tests")
    found = review.find_runs(world.app, world.store, since, world.cfg, now=e2["at"] + 60,
                             triage_store=world.tstore)  # fmt: skip
    assert [s["run"] for s in found] == [run] and found[0]["force"] is True
    world.replies.append(reviewer_reply())
    rec2 = world.review(run, sig=found[0], force=True)
    assert rec2["feedback_upto"] == e2["id"] and rec2["adopted"] == {"0": "a0123456789ab"}
    assert "and no tests" in world.asked[-1].prompt


def test_on_feedback_off(tmp_path: Path) -> None:
    w = World(tmp_path, toml='[review]\non_feedback = "off"\n')
    run = w.run(loops=0)
    feedback.add(w.app, run, "down")
    assert review.config(w.cfg)["on_feedback"] == "off"
    got = review.find_runs(w.app, w.store, time.time() - 60, w.cfg, now=time.time() + 60,
                           triage_store=w.tstore)  # fmt: skip
    assert got and got[0].get("force") is None  # found by its signals, not as feedback
    (tmp_path / "b").mkdir()
    w2 = World(tmp_path / "b", toml='[review]\nauto = "off"\n')
    run2 = w2.run(loops=0)
    feedback.add(w2.app, run2, "down")
    assert review.find_runs(w2.app, w2.store, 0, w2.cfg, now=time.time() + 60) == []


def test_thumbs_down_counts_as_trouble(world: World) -> None:
    run = world.run(loops=0)
    before = world.signals(run)["trouble"]
    feedback.add(world.app, run, "down", by="a")
    feedback.add(world.app, run, "down", by="b")
    assert world.signals(run)["trouble"] == before + 2


# -- API and CLI -----------------------------------------------------------------------------
def test_api(world: World) -> None:
    run = world.run(loops=0)
    c = TestClient(create_server(world.app, [], pipelines_dir=world.pipes))
    r = c.post(f"/api/runs/{run}/feedback", json={"rating": "down", "comment": FAKE_SECRET})
    assert r.status_code == 200, r.text
    assert r.json()["entry"]["by"] == "app" and FAKE_SECRET not in r.text
    assert r.json()["summary"]["down"] == 1
    assert c.post(f"/api/runs/{run}/feedback", json={"rating": "x"}).status_code == 400
    assert c.post("/api/runs/nope/feedback", json={"rating": "up"}).status_code == 404
    r = c.post(f"/api/runs/{run}/feedback", json={"rating": "up", "node": "write"})
    assert r.json()["entry"]["node"] == "write"
    assert len(c.get(f"/api/runs/{run}/feedback").json()["entries"]) == 2
    m = c.get(f"/api/runs/{run}/metrics").json()
    assert m["run"] == run and m["feedback"] == {"up": 1, "down": 1, "comments": 1}
    assert c.get("/api/runs/nope/metrics").status_code == 404
    md = c.get(f"/api/runs/{run}/report")
    assert md.headers["content-type"].startswith("text/markdown")
    assert f'filename="run-{run}.md"' in md.headers["content-disposition"]
    assert FAKE_SECRET not in md.text and f"# Run {run}" in md.text
    js = c.get(f"/api/runs/{run}/report?format=json")
    assert js.json()["run"]["id"] == run
    assert c.get(f"/api/runs/{run}/report?format=pdf").status_code == 422
    ins = c.get("/api/insights?days=7&pipeline=p").json()
    assert ins["pipelines"][0]["runs"] == 1 and ins["nodes"]


def test_cli(world: World, capsys: pytest.CaptureFixture[str], monkeypatch: Any) -> None:
    run = world.run(loops=0)
    db = str(world.tmp / "l.db")
    monkeypatch.setenv("AGENTLINE_USER", "cli-user")
    base = ["--db", db, "--config", str(world.tmp / "agentline.toml"), "--pipelines",
            str(world.pipes)]  # fmt: skip
    assert main([*base, "feedback", run, "down", "-m", "too slow"]) == 0
    assert "cli-user on the run: thumbs down: too slow" in capsys.readouterr().out
    assert main([*base, "feedback", run, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["down"] == 1
    with pytest.raises(SystemExit):
        main([*base, "feedback", run, "meh"])
    assert main([*base, "feedback", "nope", "up"]) == 1
    capsys.readouterr()
    assert main([*base, "runs", "metrics", run, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run"] == run
    assert main([*base, "runs", "metrics", run]) == 0
    assert "feedback 0 up / 1 down" in capsys.readouterr().out
    out = world.tmp / "r.md"
    assert main([*base, "runs", "report", run, "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith(f"# Run {run}")
    capsys.readouterr()
    assert main([*base, "insights", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pipelines"][0]["pipeline"] == "p"
