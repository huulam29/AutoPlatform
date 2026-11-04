"""Run review and harness learning: signals, the review, lessons, adoption + follow-up, probes."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger, history, lessons, probe, review
from agentline.api.editor import PipelineFiles
from agentline.api.server import create_server
from agentline.assistant import Live, check_actions, live_text, title
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.pipeline import build_pipeline, parse_spec
from agentline.registry import ActionCall, RunRequest, RunResult
from agentline.runners.function import FunctionRunner
from agentline.triage import TriageStore

from ..workspaces.helpers import git, make_remote

SPEC = """\
version: 1
name: p
nodes:
  write: { type: agent, prompt: prompts/write.md, out: draft }
  check: { type: agent, prompt: "Check {{draft.text}}", in: [draft], out: verdict }
  r: { type: router, switch: verdict.state, in: [verdict] }
  ship: { type: action, uses: noop, in: [draft] }
edges:
  - write -> check -> r
  - { from: r, to: write, when: again, max_loops: 3 }
  - { from: r, to: ship, when: ok }
"""
WRITE_PROMPT = "Write the change.\n"
BETTER_PROMPT = "Write the change. Add a CHANGELOG line.\n"


class World:
    def __init__(self, tmp_path: Path, toml: str = "", spec: str = SPEC) -> None:
        self.tmp = tmp_path
        cfg_path = tmp_path / "agentline.toml"
        cfg_path.write_text(toml)
        self.cfg = Config.load(cfg_path)
        self.reg = default_registry(self.cfg)
        self.replies: list[Any] = []
        self.asked: list[RunRequest] = []
        self.node_calls: list[RunRequest] = []
        self.loops = 1  # how many times check says "again" per run
        self._seen_checks: dict[str, int] = {}

        @self.reg.action("noop")
        async def noop(ctx: Any, call: ActionCall) -> dict[str, str]:
            return {"ok": "yes"}

        async def agent(req: RunRequest) -> RunResult:
            if req.node_id == "assistant":  # the reviewer / comparer (the Chat's harness)
                self.asked.append(req)
                return RunResult(output=self.replies.pop(0), cost_usd=0.03)
            self.node_calls.append(req)
            if req.node_id == "check":
                n = self._seen_checks.get(req.run_id, 0)
                self._seen_checks[req.run_id] = n + 1
                return RunResult(output={"state": "again" if n < self.loops else "ok"})
            if req.workspace is not None:
                (Path(req.workspace.path) / "change.txt").write_text(req.prompt)
            return RunResult(output={"text": "a draft"}, cost_usd=0.01)

        self.reg.add_runner(FunctionRunner("claude-code", agent))
        self.app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0.0)
        self.pipes = tmp_path / "pipelines"
        (self.pipes / "p" / "prompts").mkdir(parents=True)
        self.yaml = self.pipes / "p" / "pipeline.yaml"
        self.yaml.write_text(spec)
        (self.pipes / "p" / "prompts" / "write.md").write_text(WRITE_PROMPT)
        self.app.register_pipeline(
            build_pipeline(parse_spec(spec, path=self.yaml), self.reg, self.cfg)
        )
        self.files = PipelineFiles(self.pipes, self.reg, self.cfg)
        self.store = review.ReviewStore(tmp_path / "reviews")
        self.tstore = TriageStore(tmp_path / "triage")

    def run(self, loops: int = 1, budget: float | None = None) -> str:
        self.loops = loops
        sub = self.app.submit_sync("p", {"ticket": "T-1"}, budget_usd=budget)
        asyncio.run(self.app.worker(concurrency=1).run_until_idle())
        return sub.run_id

    def signals(self, run_id: str) -> dict[str, Any]:
        sig = review.signals(self.app, run_id, cfg=self.cfg, triage_store=self.tstore)
        assert sig is not None
        return sig

    def review(self, run_id: str, **kw: Any) -> dict[str, Any]:
        return asyncio.run(
            review.review(
                self.app,
                self.files,
                run_id,
                store=self.store,
                triage_store=self.tstore,
                live=Live(),
                **kw,
            )
        )

    def probe(self, rid: str, index: int, **kw: Any) -> dict[str, Any]:
        return asyncio.run(probe.probe(self.app, self.files, rid, index, store=self.store, **kw))

    def costs(self, run_id: str) -> list[dict[str, Any]]:
        with self.app.ledger.tx() as tx:
            return tx.list_costs(run_id)


def reviewer_reply(**over: Any) -> dict[str, Any]:
    yaml = SPEC  # the edit is in the prompt file
    out = {
        "summary": "One review loop: the writer never adds the CHANGELOG line the check wants.",
        "keep": False,
        "rows": [
            {
                "node": "write",
                "component": "prompt",
                "observed": "check sent the draft back once (iteration 1 -> 2)",
                "evidence": "check #1 output state again",
                "verdict": "problem",
            },
            {"node": "nope", "component": "weird", "observed": "x", "verdict": "maybe"},
        ],
        "actions": [
            {
                "type": "pipeline",
                "path": "p/pipeline.yaml",
                "node": "write",
                "confidence": "medium",
                "summary": "ask for the CHANGELOG line",
                "yaml": yaml,
                "files": {"prompts/write.md": BETTER_PROMPT},
            },
            {"type": "retry", "run": "r1"},  # not a review card: dropped
        ],
        "lesson": {
            "node": "write",
            "component": "prompt",
            "cue": "when a checker enforces a repo convention the writer is never told about",
            "lesson": "state the convention in the writer's prompt",
            "evidence": "row 1",
            "confidence": "medium",
        },
    }
    out.update(over)
    return out


# -- signals ----------------------------------------------------------------------------------
def test_signals_count_loops_and_clean_runs_have_none(tmp_path: Path) -> None:
    w = World(tmp_path)
    loopy = w.run(loops=2)
    sig = w.signals(loopy)
    assert sig["loops"] == [{"edge": "r->write[again]", "count": 2}]
    assert sig["trouble"] == 2 and review.has_signals(sig)
    assert "loop r->write[again] taken 2 time(s)" in review.signal_lines(sig)
    clean = w.signals(w.run(loops=0))
    assert clean["trouble"] == 0 and not review.has_signals(clean)


def test_find_runs_by_mode(tmp_path: Path) -> None:
    loopy_toml = ""
    w = World(tmp_path, loopy_toml)
    loopy, clean = w.run(loops=1), w.run(loops=0)
    later = time.time() + 60
    found = review.find_runs(w.app, w.store, 0, w.cfg, triage_store=w.tstore, now=later)
    assert [s["run"] for s in found] == [loopy]
    all_cfg = Config.from_dict({"review": {"auto": "all"}})
    found = review.find_runs(w.app, w.store, 0, all_cfg, triage_store=w.tstore, now=later)
    assert {s["run"] for s in found} == {loopy, clean}
    off = Config.from_dict({"review": {"auto": "off"}})
    assert review.find_runs(w.app, w.store, 0, off, triage_store=w.tstore, now=later) == []
    # just ended: give it a moment; already reviewed: skipped
    assert review.find_runs(w.app, w.store, 0, w.cfg, triage_store=w.tstore) == []
    w.store.save({**review._record(loopy, "p", False), "status": "done"})
    assert (
        review.find_runs(w.app, w.store, 0, all_cfg, triage_store=w.tstore, now=later)[0]["run"]
        == clean
    )


def test_cost_spike_against_the_median(tmp_path: Path) -> None:
    w = World(tmp_path)
    for _ in range(5):
        w.run(loops=0)
    sig = w.signals(w.run(loops=3))  # 4 writes instead of 1
    assert sig["spike"] and "cost" in sig["spike"]
    assert sig["spike"]["cost"]["median"] == pytest.approx(0.01)


# -- the review -------------------------------------------------------------------------------
def test_review_reads_the_whole_run_and_proposes_cards(tmp_path: Path) -> None:
    w = World(tmp_path)
    run_id = w.run(loops=1)
    w.replies = [reviewer_reply()]
    rec = w.review(run_id)
    assert rec["status"] == "done", rec.get("error")
    assert rec["summary"].startswith("One review loop")
    assert [r["node"] for r in rec["rows"]] == ["write", "nope"]
    assert rec["rows"][1]["component"] == "pipeline" and rec["rows"][1]["verdict"] == "confounded"
    kinds = [a["type"] for a in rec["actions"]]
    assert kinds == ["pipeline", "pipeline_lesson"]
    edit, card = rec["actions"]
    assert edit["issues"] == [] and edit["review"] == rec["id"] and edit["confidence"] == "medium"
    assert card["pipeline"] == "p/pipeline" and card["runs"] == run_id and card["issues"] == []
    prompt = w.asked[0].prompt
    assert "## SIGNALS" in prompt and "loop r->write[again] taken 1 time(s)" in prompt
    assert "### write #2" in prompt and "## FILE prompts/write.md" in prompt
    assert WRITE_PROMPT.strip() in prompt and "LESSON LIBRARY of this pipeline\nempty" in prompt
    assert [c["note"] for c in w.costs(run_id)].count("review") == 1
    # a second review without force keeps the record; with force asks again
    assert w.review(run_id)["id"] == rec["id"] and len(w.asked) == 1
    w.replies = [reviewer_reply(actions=[], lesson=None, keep=True)]
    again = w.review(run_id, force=True)
    assert again["keep"] and again["actions"] == [] and again["created_at"] == rec["created_at"]


def test_review_failures_and_budget(tmp_path: Path) -> None:
    w = World(tmp_path)
    run_id = w.run(loops=1, budget=0.01)  # the writes used it up
    assert w.review(run_id)["status"] == "skipped"
    run2 = w.run(loops=1)
    w.replies = ["no json here"]
    rec = w.review(run2)
    assert rec["status"] == "failed" and "JSON" in rec["error"]


def test_pipeline_action_that_doesnt_validate_has_issues(tmp_path: Path) -> None:
    w = World(tmp_path)
    run_id = w.run(loops=1)
    bad = reviewer_reply()
    bad["actions"][0]["yaml"] = SPEC.replace("uses: noop", "uses: nosuch.action")
    w.replies = [bad]
    rec = w.review(run_id)
    assert rec["actions"][0]["issues"]
    assert review.probe_candidates(rec, w.cfg) == []


# -- lessons ----------------------------------------------------------------------------------
def test_lesson_library_round_trip(tmp_path: Path) -> None:
    f = tmp_path / "p" / "pipeline.yaml"
    f.parent.mkdir()
    f.write_text(SPEC)
    assert lessons.path_for(f) == tmp_path / "p" / "lessons.md"
    assert lessons.path_for(tmp_path / "old.yaml") == tmp_path / "old.lessons.md"
    a = {"pipeline": "p/pipeline", "node": "write", "component": "prompt", "cue": " when  x ",
         "lesson": "do y", "evidence": "row 1", "confidence": "Medium, two runs"}  # fmt: skip
    one = lessons.add(f, "p", dict(a))
    assert (one.id, one.cue, one.confidence, one.status) == (1, "when x", "medium", "proposed")
    assert lessons.add(f, "p", dict(a)).id == 1  # the same lesson isn't added twice
    two = lessons.add(f, "p", {**a, "lesson": "do z", "adopted": True})
    assert two.id == 2 and two.status == "adopted"
    text = lessons.path_for(f).read_text(encoding="utf-8")
    assert text.startswith("# Lessons: p") and "### L2 · write · prompt" in text
    # a hand-written note survives a status change
    note = text.replace("- status: adopted", "- status: adopted\nmy note")
    lessons.path_for(f).write_text(note, encoding="utf-8")
    lessons.set_status(f, "p", 2, "helped", "trouble 1.0 -> 0.0")
    got = {x.id: x for x in lessons.read(f)}
    assert got[2].status == "helped" and "my note" in got[2].notes
    assert any("trouble 1.0 -> 0.0" in n for n in got[2].notes)
    with pytest.raises(Exception, match="status must be"):
        lessons.set_status(f, "p", 2, "great")
    assert lessons.check({"pipeline": "p", "node": "", "cue": "", "lesson": ""})


# -- adoption and follow-up -------------------------------------------------------------------
def test_adopt_then_follow_up_helped(tmp_path: Path) -> None:
    w = World(tmp_path)
    for _ in range(2):
        w.run(loops=1)
    run_id = w.run(loops=1)
    w.replies = [reviewer_reply()]
    rec = w.review(run_id)
    lessons.add(w.yaml, "p", {**rec["actions"][1]})
    ad = review.apply_edit(w.app, w.files, rec, 0, w.store)
    assert (w.pipes / "p" / "prompts" / "write.md").read_text() == BETTER_PROMPT
    assert ad["before"]["files"] == {"prompts/write.md": WRITE_PROMPT} and ad["commit"]
    assert ad["baseline"]["runs"] == 3 and ad["baseline"]["trouble"] == 1.0
    assert ad["node"] == "write" and ad["component"] == "prompt" and ad["lessons"] == [1]
    assert lessons.read(w.yaml)[0].status == "adopted"
    again = review.apply_edit(w.app, w.files, w.store.get(rec["id"]), 0, w.store)
    assert again["id"] == ad["id"]
    time.sleep(0.01)
    for _ in range(4):
        w.run(loops=0)
    assert review.follow_up(w.app, w.files, w.store) == []  # 4 of 5 runs
    assert w.store.adoptions.get(ad["id"])["after"]["runs"] == 4
    w.run(loops=0)
    (done,) = review.follow_up(w.app, w.files, w.store)
    assert done["verdict"] == "helped" and "trouble 1.0 -> 0.0" in done["detail"]
    assert done["actions"] == []
    assert lessons.read(w.yaml)[0].status == "helped"


def test_follow_up_hurt_offers_a_restore(tmp_path: Path) -> None:
    w = World(tmp_path, "[review]\nfollowup_runs = 2\n")
    run_id = w.run(loops=0)
    w.replies = [reviewer_reply()]
    rec = w.review(run_id, force=True)
    review.apply_edit(w.app, w.files, rec, 0, w.store)
    time.sleep(0.01)
    w.run(loops=2)
    w.run(loops=2)
    (done,) = review.follow_up(w.app, w.files, w.store)
    assert done["verdict"] == "hurt"
    (card,) = done["actions"]
    assert card["type"] == "pipeline" and card["issues"] == []
    assert card["files"] == {"prompts/write.md": WRITE_PROMPT} and card["yaml"] == SPEC


def test_judge() -> None:
    def j(b: float, a: float) -> str:
        return review.judge({"trouble": b}, {"trouble": a, "runs": 5})[0]

    assert j(2, 0.4) == "helped" and j(0.4, 2) == "hurt"
    assert j(1, 0.8) == "no effect"  # less than 0.5 per run
    assert j(10, 9) == "no effect"  # less than 30 %


# -- probes -----------------------------------------------------------------------------------
def _compare(rec: dict[str, Any], index: int, candidate_wins: bool) -> dict[str, str]:
    import hashlib

    pid = "p" + hashlib.sha1(f"{rec['id']}:{index}".encode()).hexdigest()[:12]
    first = probe.candidate_first(pid)
    better = "A" if first == candidate_wins else "B"
    return {"better": better, "reasoning": "the edited one adds the CHANGELOG line"}


def test_probe_replays_the_node_with_the_edit(tmp_path: Path) -> None:
    w = World(tmp_path)
    run_id = w.run(loops=1)
    w.replies = [reviewer_reply()]
    rec = w.review(run_id)
    w.node_calls.clear()
    w.replies = [_compare(rec, 0, candidate_wins=True)]
    p = w.probe(rec["id"], 0)
    assert p["status"] == "done", p.get("error")
    assert p["verdict"] == "better" and p["node"] == "write" and p["iteration"] == 1
    (call,) = w.node_calls  # only the edited node ran, with the new prompt
    assert call.node_id == "write" and call.prompt == BETTER_PROMPT
    assert call.run_id == f"probe-{run_id}"
    assert "CHANGELOG" in p["edit"] and p["samples"] == 1
    compare = w.asked[-1].prompt
    assert "## PROBLEM" in compare and "check sent the draft back once" in compare
    saved = w.store.get(rec["id"])
    assert saved["probes"]["0"]["verdict"] == "better"
    assert saved["actions"][0]["probe"]["verdict"] == "better"
    assert saved["probe_cost_usd"] == pytest.approx(0.04)  # node 0.01 + compare 0.03
    assert [c["note"] for c in w.costs(run_id)].count("probe") == 2
    # done: not redone without force; worse when the original wins
    assert w.probe(rec["id"], 0)["verdict"] == "better"
    w.replies = [_compare(rec, 0, candidate_wins=False)]
    assert w.probe(rec["id"], 0, force=True)["verdict"] == "worse"


def test_probe_skips(tmp_path: Path) -> None:
    w = World(tmp_path, "[review]\nprobe_budget_usd = 0\n")
    run_id = w.run(loops=1)
    w.replies = [reviewer_reply()]
    rec = w.review(run_id)
    assert w.probe(rec["id"], 0)["status"] == "skipped"
    assert "probe budget" in w.store.get(rec["id"])["probes"]["0"]["error"]
    (tmp_path / "b").mkdir()
    w2 = World(tmp_path / "b")
    run2 = w2.run(loops=1)
    nothing = reviewer_reply()
    nothing["actions"][0]["files"] = {}  # changes nothing
    w2.replies = [nothing]
    rec2 = w2.review(run2)
    p = w2.probe(rec2["id"], 0)
    assert p["status"] == "skipped" and "changes no agent node" in p["error"]
    assert w2.probe(rec2["id"], 1)["status"] == "skipped"  # the lesson card


def test_probe_coding_node_in_a_clone_without_remote(tmp_path: Path) -> None:
    remote = make_remote(tmp_path / "remote")
    spec = SPEC.replace(
        "write: { type: agent, prompt: prompts/write.md, out: draft }",
        "write: { type: agent, prompt: prompts/write.md, out: draft,"
        " workspace: { type: local, repo: app } }",
    )
    remote_url = Path(remote).as_posix()  # a Windows path's backslashes are TOML escapes
    w = World(tmp_path, f'[repos.app]\nremote = "{remote_url}"\n', spec=spec)
    run_id = w.run(loops=1)
    run = w.app.get_run(run_id)
    assert run is not None and run.status == "done"
    mirror = Path(w.app.result(run_id, "write")["mirror"])
    branch = f"agentline/{run_id}"
    before = git(mirror, "rev-parse", branch)
    reply = reviewer_reply()
    reply["actions"][0]["yaml"] = spec
    w.replies = [reply]
    rec = w.review(run_id)
    seen: dict[str, Any] = {}
    original = w.reg.get_runner("claude-code")

    async def look(req: RunRequest) -> RunResult:
        if req.node_id == "write":
            cwd = Path(req.workspace.path)  # type: ignore[union-attr]
            seen["remotes"] = subprocess.run(["git", "remote"], cwd=cwd, capture_output=True,
                                             text=True).stdout.strip()  # fmt: skip
            seen["path"] = cwd
        return await original.run(req)

    w.reg.add_runner(FunctionRunner("claude-code", look))
    w.replies = [_compare(rec, 0, candidate_wins=True)]
    p = w.probe(rec["id"], 0)
    assert p["status"] == "done", p.get("error")
    assert seen["remotes"] == ""  # nothing can be pushed
    assert not seen["path"].exists()  # the clone is gone
    assert "change.txt" in p["candidate"]["diff"] and "CHANGELOG" in p["candidate"]["diff"]
    assert git(mirror, "rev-parse", branch) == before  # the run's branch is untouched


def test_probe_mcp_off_unless_probe_safe(tmp_path: Path) -> None:
    toml = (
        '[mcp.docs]\ncommand = "docs-mcp"\nprobe_safe = true\n\n'
        '[mcp.jira]\ncommand = "jira-mcp"\nprobe_safe = false\n'
    )
    spec = SPEC.replace(
        "write: { type: agent, prompt: prompts/write.md, out: draft }",
        "write: { type: agent, prompt: prompts/write.md, out: draft, mcp: [docs, jira] }",
    )
    w = World(tmp_path, toml, spec=spec)
    run_id = w.run(loops=1)
    assert set(w.node_calls[0].params["mcp_servers"]) == {"docs", "jira"}
    reply = reviewer_reply()
    reply["actions"][0]["yaml"] = spec
    w.replies = [reply]
    rec = w.review(run_id)
    w.node_calls.clear()
    w.replies = [_compare(rec, 0, candidate_wins=True)]
    p = w.probe(rec["id"], 0)
    assert p["status"] == "done", p.get("error")
    assert set(w.node_calls[0].params["mcp_servers"]) == {"docs"}
    assert "MCP off in the probe: jira" in p["notes"]


def test_pick_execution() -> None:
    from types import SimpleNamespace as N

    one, two = N(node_id="w", iteration=1, status="done"), N(node_id="w", iteration=2,
                                                             status="done")  # fmt: skip
    assert probe.pick_execution([two, one], "w") is one
    bad = N(node_id="w", iteration=2, status="dead")
    assert probe.pick_execution([one, bad], "w") is bad
    assert probe.pick_execution([one], "x") is None


# -- API, Chat, chat links --------------------------------------------------------------------
def test_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(tmp_path)
    monkeypatch.setattr(review, "reviews_dir", lambda: tmp_path / "reviews")
    run_id = w.run(loops=1)
    server = create_server(w.app, [], pipelines_dir=w.pipes, registry=w.reg, config=w.cfg)
    c = TestClient(server)
    w.replies = [reviewer_reply()]
    rec = c.post("/api/reviews", json={"run": run_id}).json()
    assert rec["status"] == "done"
    listed = c.get(f"/api/reviews?run={run_id}").json()
    assert [r["id"] for r in listed["records"]] == [rec["id"]]
    assert listed["config"]["auto"] == "signals"
    assert c.get(f"/api/reviews/{rec['id']}").json()["id"] == rec["id"]
    assert c.post("/api/reviews", json={"run": "nope"}).status_code == 404
    assert c.get("/api/lessons").json() == {"pipelines": []}  # nothing learned yet
    w.replies = [_compare(rec, 0, candidate_wins=True)]
    p = c.post(f"/api/reviews/{rec['id']}/probe", json={"index": 0}).json()
    assert p["verdict"] == "better"
    assert c.post(f"/api/reviews/{rec['id']}/probe", json={"index": 9}).status_code == 404
    card = rec["actions"][1]
    body = {k: card[k] for k in ("node", "component", "cue", "lesson", "evidence", "confidence")}
    got = c.post("/api/lessons/p/pipeline", json={**body, "review": rec["id"]}).json()
    assert got["id"] == 1
    assert c.get(f"/api/reviews/{rec['id']}").json()["lesson_added"] == 1  # card shows applied
    status = {"op": "status", "id": 1, "status": "dismissed"}
    assert c.post("/api/lessons/p/pipeline", json=status).json()["status"] == "dismissed"
    missing = {**status, "id": 7}
    assert c.post("/api/lessons/p/pipeline", json=missing).status_code == 404
    ad = c.post(f"/api/reviews/{rec['id']}/apply", json={"index": 0}).json()
    assert ad["node"] == "write"
    assert c.post(f"/api/reviews/{rec['id']}/apply", json={"index": 1}).status_code == 422
    assert c.get("/api/adoptions?flow=p").json()["adoptions"][0]["id"] == ad["id"]
    lib = c.get("/api/lessons/p/pipeline").json()
    assert lib["flow"] == "p" and lib["file"] == "p/lessons.md"
    assert [x["id"] for x in lib["lessons"]] == [1] and lib["adoptions"][0]["id"] == ad["id"]
    assert c.get("/api/lessons/nope").status_code == 404
    every = c.get("/api/lessons").json()["pipelines"]  # the Lessons page
    assert [(x["pipeline"], len(x["lessons"]), len(x["adoptions"])) for x in every] == [
        ("p/pipeline", 1, 1)
    ]
    assert history.log(w.pipes, "p/lessons.md")  # lesson changes are in history


def test_chat_actions(tmp_path: Path) -> None:
    w = World(tmp_path)
    live = Live(reviews=[{"id": "v0123456789ab", "run": "r", "flow": "p", "status": "done",
                          "summary": "s", "proposals": [{"index": 0, "type": "pipeline",
                          "title": "update pipeline", "probe": "better"},
                          {"index": 1, "type": "pipeline_lesson", "title": "l",
                          "probe": None}]}])  # fmt: skip
    acts = [
        {"type": "review", "run": "r_1"},
        {"type": "review", "run": "bad id!"},
        {"type": "probe", "review": "v0123456789ab", "index": 0},
        {"type": "probe", "review": "v0123456789ab", "index": 1},
        {"type": "probe", "review": "vzz", "index": 0},
        {"type": "pipeline_lesson", "pipeline": "p/pipeline", "node": "write", "cue": "c",
         "lesson": "l", "confidence": "high"},
        {"type": "pipeline_lesson", "pipeline": "nope", "node": "write", "cue": "c",
         "lesson": "l"},
    ]  # fmt: skip
    got = check_actions(acts, w.pipes, w.reg, w.cfg, live)
    assert [bool(a["issues"]) for a in got] == [False, True, False, True, True, False, True]
    assert title(got[2]) == "probe proposal 0 of review v0123456789ab"
    assert title(got[5]) == "add a lesson for write to p/pipeline"
    text = live_text(live)
    assert "REVIEWS" in text and "[0] update pipeline (probed: better)" in text


def test_live_state_has_the_open_pipelines_lessons(tmp_path: Path) -> None:
    from agentline.assistant import gather_live

    w = World(tmp_path)
    lessons.add(w.yaml, "p", {"pipeline": "p/pipeline", "node": "write", "cue": "when c",
                              "lesson": "do l"})  # fmt: skip
    live = gather_live(w.files, w.app, pipeline="p/pipeline")
    assert "when c" in live.lessons["p/pipeline"]
    assert "LESSONS of pipeline p/pipeline" in live_text(live)


def test_chat_text(tmp_path: Path) -> None:
    rec = {"flow": "p", "run": "r1", "summary": "one loop", "keep": False,
           "actions": [{"type": "pipeline", "path": "p/pipeline.yaml", "exists": True,
                        "issues": []}]}  # fmt: skip
    text = review.chat_text(rec)
    assert "Review of p run r1" in text and "update pipeline p/pipeline.yaml" in text


def test_loop_reviews_and_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(tmp_path)
    monkeypatch.setattr(review, "SETTLE", 0.0)
    w.store.since()  # automatic review began before the run
    time.sleep(0.01)
    run_id = w.run(loops=1)
    rid = review.review_id(run_id)
    w.replies = [reviewer_reply(), _compare({"id": rid}, 0, candidate_wins=True)]
    stop = asyncio.Event()

    async def until_probed() -> None:
        while ((w.store.get(rid) or {}).get("probes") or {}).get("0", {}).get("status") not in (
            "done", "failed", "skipped",
        ):  # fmt: skip
            await asyncio.sleep(0.02)
        stop.set()

    async def go() -> None:
        loop = review.run_review_loop(w.app, w.files, stop, store=w.store, interval=0.02)
        await asyncio.wait_for(asyncio.gather(loop, until_probed()), 10)

    asyncio.run(go())
    rec = w.store.get(rid)
    assert rec["status"] == "done" and rec["probes"]["0"]["status"] == "done"
    assert json.dumps(rec)  # the record is plain JSON


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from agentline.cli import main

    w = World(tmp_path)
    run_id = w.run(loops=1)
    w.replies = [reviewer_reply()]
    rec = asyncio.run(review.review(w.app, w.files, run_id, live=Live()))  # the default store
    lessons.add(w.yaml, "p", dict(rec["actions"][1]))
    assert main(["review", "list"]) == 0
    assert f"run {run_id} of p: done" in capsys.readouterr().out
    assert main(["review", "show", rec["id"]]) == 0
    out = capsys.readouterr().out
    assert "signal: loop r->write[again] taken 1 time(s)" in out
    assert "problem: write prompt: check sent the draft back once" in out
    assert "proposal 0: update pipeline p/pipeline.yaml" in out
    assert main(["review", "show", "v000000000000"]) == 1
    assert main(["lessons", "p/pipeline", "--pipelines", str(w.pipes)]) == 0
    assert "L1 [proposed] write prompt: when a checker enforces" in capsys.readouterr().out


def test_auto_review_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_REVIEW", "off")
    assert not review.auto_enabled()
    monkeypatch.setenv("AGENTLINE_REVIEW", "")
    assert review.auto_enabled()
