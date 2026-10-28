"""Failure triage: find failures, classify with a fake agent, proposals, reports, GitHub."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger, triage
from agentline.api.editor import PipelineFiles
from agentline.api.server import create_server
from agentline.assistant import Live, check_actions, title
from agentline.builtins import default_registry
from agentline.chat_instructions import LESSONS, add_lesson, read
from agentline.config import Config
from agentline.issues import IssueStore
from agentline.pipeline import build_pipeline, parse_spec
from agentline.registry import ActionCall, RunRequest, RunResult
from agentline.runners.function import FunctionRunner

SPEC = """\
version: 1
name: p
nodes:
  plan: { type: agent, prompt: prompts/plan.md, out: plan }
  build: { type: action, uses: boom, in: [plan], max_attempts: 1 }
edges:
  - plan -> build
"""
FIXED = SPEC.replace(
    "{ type: agent, prompt: prompts/plan.md, out: plan }",
    "{ type: agent, prompt: prompts/plan.md, output_schema: schemas/plan.json, out: plan }",
)
SECRET = "sk-test-supersecret-123"


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:  # skip socket.getfqdn() (slow on macOS)
        import socketserver

        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]  # type: ignore[misc]


@pytest.fixture
def github() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """A fake GitHub: POST /repos/o/r/issues and /issues/<n>/comments."""
    seen: list[dict[str, Any]] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "body": body, "auth": self.headers["Authorization"]})
            n = sum(1 for s in seen if s["path"].endswith("/issues"))
            out = {"number": n, "html_url": f"https://github.test/o/r/issues/{n}"}
            data = json.dumps(out).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = _Server(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    finally:
        srv.shutdown()


class World:
    def __init__(self, tmp_path: Path, toml: str = "", error: str = "the plan has no steps"):
        self.tmp = tmp_path
        cfg_path = tmp_path / "agentline.toml"
        cfg_path.write_text(toml)
        self.cfg = Config.load(cfg_path)
        self.reg = default_registry(self.cfg)
        self.replies: list[Any] = []
        self.seen: list[RunRequest] = []
        self.plan_output: Any = {"summary": "a plan", "steps": []}

        @self.reg.action("boom")
        async def boom(ctx: Any, call: ActionCall) -> dict[str, str]:
            raise RuntimeError(error)

        async def agent(req: RunRequest) -> RunResult:
            if req.node_id == "plan":  # the pipeline's own agent node
                return RunResult(output=self.plan_output)
            self.seen.append(req)
            return RunResult(output=self.replies.pop(0), cost_usd=0.02)

        self.reg.add_runner(FunctionRunner("claude-code", agent))
        self.app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0.0)
        self.app.redactor.add("TEST_SECRET", SECRET)
        self.pipes = tmp_path / "pipelines"
        (self.pipes / "prompts").mkdir(parents=True)
        (self.pipes / "p.yaml").write_text(SPEC)
        (self.pipes / "prompts" / "plan.md").write_text("Plan the ticket.\n")
        spec = parse_spec(SPEC, base_dir=self.pipes)
        self.app.register_pipeline(build_pipeline(spec, self.reg))
        self.files = PipelineFiles(self.pipes, self.reg, self.cfg)
        self.issues = IssueStore(tmp_path / "issues")
        self.store = triage.TriageStore(tmp_path / "triage")

    def run(self, budget: float | None = None) -> str:
        sub = self.app.submit_sync("p", {}, budget_usd=budget)
        asyncio.run(self.app.worker(concurrency=1).run_until_idle())
        return sub.run_id

    def triage(self, run_id: str, **kw: Any) -> dict[str, Any]:
        f = triage.failure_for(self.app, run_id)
        assert f is not None
        return asyncio.run(
            triage.triage(self.app, self.files, f, store=self.store, issues=self.issues, **kw)
        )

    def client(self) -> TestClient:
        server = create_server(
            self.app, [], pipelines_dir=self.pipes, registry=self.reg, config=self.cfg
        )
        return TestClient(server)


AI_REPLY = {
    "verdict": "ai-instruction",
    "summary": "The plan node returned no steps, so build had nothing to do.",
    "reasoning": "The plan prompt never asks for steps and there is no output_schema.",
    "node": "plan",
    "lesson": "Give agent nodes whose output later nodes read an output_schema.",
    "actions": [
        {"type": "pipeline", "path": "p.yaml", "summary": "add a schema", "yaml": FIXED,
         "files": {"schemas/plan.json": '{"type": "object", "required": ["steps"]}'}},
    ],
}  # fmt: skip
INFRA_REPLY = {
    "verdict": "infrastructure",
    "summary": "The worker lost the result of build.",
    "reasoning": f"The traceback is inside agentline's scheduler (token {SECRET}).",
    "node": "build",
    "report": {"title": "Scheduler drops results", "body": f"Seen with key {SECRET}."},
    "actions": [{"type": "retry", "run": "x", "node": "build"}],
}


def test_ai_instruction_verdict_proposes_edit_lesson_and_retry(tmp_path: Path) -> None:
    w = World(tmp_path)
    run_id = w.run()
    with w.app.ledger.tx() as tx:
        before = tx.get_run(run_id)
    assert before is not None and before.status == "dead"
    found = triage.find_failures(w.app, w.store, since=0)
    assert [(f.node, f.trigger) for f in found] == [("build", "dead")]
    w.replies.append(AI_REPLY)
    rec = w.triage(run_id)
    assert rec["status"] == "done" and rec["verdict"] == "ai-instruction"
    assert rec["node"] == "build" and rec["cost_usd"] == 0.02
    kinds = [a["type"] for a in rec["actions"]]
    assert kinds == ["pipeline", "lesson", "retry"]
    assert all(a["issues"] == [] for a in rec["actions"]), [a["issues"] for a in rec["actions"]]
    assert rec["actions"][2] == {**rec["actions"][2], "run": run_id, "node": "build"}
    # the prompt carries the evidence, the pipeline, the node's files and the instructions
    prompt = w.seen[0].prompt
    for part in ("the plan has no steps", "## PIPELINE FILE p.yaml", "CURRENT CHAT INSTRUCTIONS",
                 "interview-me", "trigger: dead", "prompts/plan.md"):  # fmt: skip
        assert part in prompt, part
    # triage counts against the run's budget, without looking like new activity
    with w.app.ledger.tx() as tx:
        after = tx.get_run(run_id)
        costs = tx.list_costs(run_id)
    assert after is not None and after.cost_usd == pytest.approx(0.02)
    assert after.updated_at == before.updated_at and costs[-1]["note"] == "triage"
    # Problems: the run's issue carries the verdict
    issue = w.issues.by_source()[f"run:{run_id}"]
    assert issue["triage"]["verdict"] == "ai-instruction" and rec["issue"] == issue["number"]
    assert issue["notes"][-1]["text"].startswith("Triage: ai-instruction")
    # one triage per failure: nothing new to find, and asking again returns the record
    assert triage.find_failures(w.app, w.store, since=0) == []
    assert w.triage(run_id)["id"] == rec["id"] and len(w.seen) == 1


def test_node_prompt_file_goes_into_the_prompt(tmp_path: Path) -> None:
    w = World(tmp_path)
    run_id = w.run()
    f = triage.failure_for(w.app, run_id, "plan")
    assert f is not None and f.trigger == "empty-output"  # plan is done; asked by node
    ev = triage.evidence(w.app, w.files, f)
    assert ev["path"] == "p.yaml" and ev["files"] == [("prompts/plan.md", "Plan the ticket.\n")]


def test_empty_agent_output_is_a_failure(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.plan_output = {"steps": [], "summary": " "}
    w.run()
    found = triage.find_failures(w.app, w.store, since=0)
    assert sorted((f.node, f.trigger) for f in found) == [
        ("build", "dead"),
        ("plan", "empty-output"),
    ]
    checked: set[str] = set()
    triage.find_failures(w.app, w.store, since=0, checked=checked)
    assert len(checked) == 1  # the result is loaded once
    w.plan_output = {"steps": ["a"]}
    w.run()
    assert not [f for f in triage.find_failures(w.app, w.store, 0, checked)
                if f.trigger == "empty-output" and f.run_id != found[0].run_id]  # fmt: skip


def test_infrastructure_report_is_redacted_and_local_without_github(tmp_path: Path) -> None:
    w = World(tmp_path, error=f"KeyError in scheduler with {SECRET}")
    run_id = w.run()
    w.replies.append(INFRA_REPLY)
    rec = w.triage(run_id)
    assert rec["verdict"] == "infrastructure" and rec["lesson"] == ""
    md = rec["report"]["markdown"]
    assert SECRET not in json.dumps(rec) and "[REDACTED:TEST_SECRET]" in md
    assert "## What happened" in md and "Scheduler drops results" in rec["report"]["title"]
    assert "agentline-triage:" in md
    assert [a["type"] for a in rec["actions"]] == ["retry"]  # no GitHub card without a repo
    assert rec["actions"][0]["run"] == "x"  # the agent's run id is checked, not replaced
    issue = w.issues.get(rec["issue"])
    assert issue is not None and issue["triage"]["verdict"] == "infrastructure"
    assert issue["notes"][-1]["text"].endswith("(report saved with the triage)")
    st = triage.github_status(w.cfg)
    assert st == {"repo": None, "token_set": False, "ready": False, "auto": False, "error": None}
    with pytest.raises(Exception, match="isn't set up"):
        triage.report_github(rec, w.cfg, w.store)


def test_github_issue_card_creates_once_and_comments_on_a_repeat(
    tmp_path: Path, github: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, seen = github
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-token")
    monkeypatch.setenv("GITHUB_API_URL", url)
    w = World(tmp_path, toml='[report]\ngithub_repo = "o/r"\n')
    run_id = w.run()
    w.replies.append(INFRA_REPLY)
    c = w.client()
    # the click path: POST /api/triage, then the card's POST .../github
    r = c.post("/api/triage", json={"run": run_id})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["manual"] and rec["actions"][-1]["type"] == "report_issue"
    assert c.get("/api/triage").json()["github"]["ready"] is True
    gh = c.post(f"/api/triage/{rec['id']}/github").json()["github"]
    assert gh == {"repo": "o/r", "number": 1, "url": "https://github.test/o/r/issues/1"}
    assert seen[0]["path"] == "/repos/o/r/issues" and seen[0]["auth"] == "Bearer ghp-test-token"
    assert seen[0]["body"]["title"].startswith("[triage] Scheduler drops results")
    assert seen[0]["body"]["labels"] == ["agentline", "triage"]
    assert c.post(f"/api/triage/{rec['id']}/github").json()["github"]["number"] == 1
    assert len(seen) == 1  # once
    # the same failure in another run: a comment on the same issue
    run2 = w.run()
    w.replies.append(INFRA_REPLY)
    rec2 = c.post("/api/triage", json={"run": run2}).json()
    assert rec2["fingerprint"] == rec["fingerprint"]
    c.post(f"/api/triage/{rec2['id']}/github")
    assert seen[-1]["path"] == "/repos/o/r/issues/1/comments"
    assert "Happened again" in seen[-1]["body"]["body"]
    assert c.post("/api/triage", json={"run": "nope"}).status_code == 404
    assert c.get("/api/triage/t000000000000").status_code == 404


def test_github_auto_reports_right_away(
    tmp_path: Path, github: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, seen = github
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-token")
    monkeypatch.setenv("GITHUB_API_URL", url)
    w = World(tmp_path, toml='[report]\ngithub_repo = "o/r"\ngithub_auto = true\n')
    run_id = w.run()
    w.replies.append(INFRA_REPLY)
    rec = w.triage(run_id)
    assert rec["github"]["number"] == 1 and len(seen) == 1
    assert w.issues.get(rec["issue"])["triage"]["github"]["number"] == 1  # type: ignore[index]


def test_used_up_budget_skips_triage(tmp_path: Path) -> None:
    w = World(tmp_path)
    run_id = w.run(budget=0.01)
    with w.app.ledger.tx() as tx:
        x = tx.list_executions(run_id)[0]
        tx.add_cost(run_id, x.id, 1, 0.01, "agent")
    rec = w.triage(run_id)
    assert rec["status"] == "skipped" and "budget is used up" in rec["error"]
    assert w.seen == []


def test_a_bad_answer_fails_and_a_click_redoes_it(tmp_path: Path) -> None:
    w = World(tmp_path)
    run_id = w.run()
    w.replies.append("no idea")
    assert w.triage(run_id)["status"] == "failed"
    w.replies.append({"verdict": "external", "summary": "The API key was revoked.",
                      "actions": [{"type": "key", "name": "OPENROUTER_API_KEY"}]})  # fmt: skip
    rec = w.triage(run_id)  # asking again redoes a failed triage
    assert rec["verdict"] == "external" and [a["type"] for a in rec["actions"]] == ["key", "retry"]
    w.replies.append(AI_REPLY)
    assert w.triage(run_id, force=True)["verdict"] == "ai-instruction"


def test_pipeline_edit_only_for_ai_instruction(tmp_path: Path) -> None:
    w = World(tmp_path)
    run_id = w.run()
    w.replies.append({**AI_REPLY, "verdict": "external", "lesson": "x"})
    rec = w.triage(run_id)
    assert [a["type"] for a in rec["actions"]] == ["retry"] and rec["lesson"] == ""


def test_the_loop_triages_new_failures_only(tmp_path: Path) -> None:
    w = World(tmp_path)
    old = w.run()  # before automatic triage began: left alone
    w.store.since()

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(
            triage.run_triage_loop(
                w.app, w.files, stop, store=w.store, issues=w.issues, interval=0.05
            )
        )
        await asyncio.sleep(0.2)
        assert w.store.list() == []
        sub = w.app.submit_sync("p", {})
        w.replies.append(AI_REPLY)
        await w.app.worker(concurrency=1).run_until_idle()
        for _ in range(100):
            if w.store.list():
                break
            await asyncio.sleep(0.05)
        stop.set()
        await loop
        assert [r["run"] for r in w.store.list()] == [sub.run_id]

    asyncio.run(go())
    assert all(r["run"] != old for r in w.store.list())


def test_lessons_are_appended_once_under_their_heading() -> None:
    add_lesson("Give planning nodes an output_schema.")
    add_lesson("  Give planning nodes   an output_schema. ")
    add_lesson("Prefer script steps for lint.")
    text = read()
    assert text.count("Give planning nodes an output_schema.") == 1
    lines = text.splitlines()
    i = lines.index(LESSONS)
    assert lines[i + 1 : i + 3] == [
        "- Give planning nodes an output_schema.",
        "- Prefer script steps for lint.",
    ]
    assert "interview-me" in text  # the rest stays
    with pytest.raises(Exception, match="one line"):
        add_lesson("x" * 301)


def test_lesson_api(tmp_path: Path) -> None:
    w = World(tmp_path)
    c = w.client()
    r = c.post("/api/chat-instructions/lesson", json={"text": "Check inputs first."})
    assert r.status_code == 200 and "- Check inputs first." in r.json()["text"]
    assert c.post("/api/chat-instructions/lesson", json={"text": " "}).status_code == 422


def test_chat_actions_for_triage_are_checked(tmp_path: Path) -> None:
    live = Live(triage=[{"id": "t0123456789ab", "verdict": "infrastructure"},
                        {"id": "t0123456789ac", "verdict": "external"}])  # fmt: skip
    acts = check_actions(
        [
            {"type": "triage", "run": "r-1", "node": "build"},
            {"type": "triage", "run": "../x"},
            {"type": "lesson", "text": "  one   line  "},
            {"type": "lesson", "text": ""},
            {"type": "report_issue", "triage": "t0123456789ab"},
            {"type": "report_issue", "triage": "t0123456789ac"},
            {"type": "report_issue", "triage": "t0123456789ff"},
        ],
        tmp_path,
        None,
        None,
        live,
    )
    assert [bool(a["issues"]) for a in acts] == [False, True, False, True, False, True, True]
    assert acts[2]["text"] == "one line"
    assert title(acts[0]) == "triage run r-1 at build"
    assert title(acts[4]) == "report it on GitHub"


def test_chat_text_lists_the_proposals() -> None:
    rec = {"flow": "p", "run": "r1", "node": "build", "verdict": "ai-instruction",
           "summary": "No steps.", "github": None,
           "actions": [{"type": "lesson", "text": "x", "issues": []},
                       {"type": "retry", "run": "r1", "node": "build", "issues": []}]}  # fmt: skip
    text = triage.chat_text(rec)
    assert text.startswith("🔎 Triage of p run r1 at build: ai-instruction")
    assert "add a lesson to the Chat instructions; retry run r1 at build" in text


def test_cli_lists_and_shows_triage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from agentline.cli import main

    w = World(tmp_path)
    run_id = w.run()
    w.replies.append(AI_REPLY)
    f = triage.failure_for(w.app, run_id)
    assert f is not None
    rec = asyncio.run(triage.triage(w.app, w.files, f))  # the default store ($AGENTLINE_HOME)
    assert main(["triage", "list"]) == 0
    assert f"run {run_id} build: ai-instruction" in capsys.readouterr().out
    assert main(["triage", "show", rec["id"]]) == 0
    out = capsys.readouterr().out
    assert "proposal: add a lesson to the Chat instructions" in out
    assert main(["triage", "show", "t000000000000"]) == 1


def test_auto_triage_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_TRIAGE", "off")
    assert not triage.auto_enabled()
    monkeypatch.setenv("AGENTLINE_TRIAGE", "")
    assert triage.auto_enabled()


# -- app problems (incidents) and App reports ---------------------------------------------------
def _incidents(tmp_path: Path) -> Any:
    from agentline.incidents import IncidentStore

    return IncidentStore(tmp_path / "incidents")


def test_crash_reports_become_infrastructure_reports_without_a_model(tmp_path: Path) -> None:
    w = World(tmp_path)
    inc = _incidents(tmp_path)
    rec0 = inc.record("worker_error", title="TypeError in step", message=f"bad {SECRET}")
    [rec] = triage.triage_incidents(inc, w.store, w.cfg, w.app.redactor)
    assert rec["verdict"] == "infrastructure" and rec["incident"] == rec0["id"]
    assert rec["summary"] == "TypeError in step" and rec["run"] is None and w.seen == []
    md = rec["report"]["markdown"]
    assert "## What happened" in md and SECRET not in md
    assert rec["actions"] == []  # no GitHub set up: copy it from App reports
    assert triage.triage_incidents(inc, w.store, w.cfg) == []  # once per occurrence count
    inc.record("worker_error", title="TypeError in step", message=f"bad {SECRET}")  # again
    [again] = triage.triage_incidents(inc, w.store, w.cfg)
    assert again["incident_count"] == 2 and again["created_at"] == rec["created_at"]
    assert "crash report" in triage.chat_text(again)
    inc.delete(rec0["id"])  # the report is gone: so is its record
    triage.triage_incidents(inc, w.store, w.cfg)
    assert w.store.get(rec["id"]) is None


def test_crash_report_goes_to_github(
    tmp_path: Path, github: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, seen = github
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-token")
    monkeypatch.setenv("GITHUB_API_URL", url)
    w = World(tmp_path, toml='[report]\ngithub_repo = "o/r"\n')
    inc = _incidents(tmp_path)
    inc.record("api_error", title="KeyError: 'x'", message="boom")
    [rec] = triage.triage_incidents(inc, w.store, w.cfg)
    assert [a["type"] for a in rec["actions"]] == ["report_issue"]
    done = triage.report_github(rec, w.cfg, w.store)
    assert done["github"]["number"] == 1 and seen[0]["body"]["title"] == "[triage] KeyError: 'x'"
    inc.record("api_error", title="KeyError: 'x'", message="boom")
    [again] = triage.triage_incidents(inc, w.store, w.cfg)
    assert again["github"]["number"] == 1 and again["actions"] == []  # reported already


def test_app_reports_api_and_problems_stay_user_fixable(tmp_path: Path) -> None:
    from agentline.incidents import IncidentStore

    w = World(tmp_path, error="KeyError in scheduler")
    inc = IncidentStore(tmp_path / "home" / "incidents")
    w.app.incidents = inc
    inc.record("app_crash", title="Crash: EXC_BREAKPOINT", message="trap")
    infra_run, ai_run = w.run(), w.run()
    c = w.client()
    w.replies.append(INFRA_REPLY)
    assert c.post("/api/triage", json={"run": infra_run}).json()["verdict"] == "infrastructure"
    w.replies.append(AI_REPLY)
    assert c.post("/api/triage", json={"run": ai_run}).json()["verdict"] == "ai-instruction"
    body = c.get("/api/app-reports").json()
    kinds = sorted("incident" if r.get("incident") else r["run"] for r in body["reports"])
    assert kinds == sorted(["incident", infra_run]) and body["github"]["ready"] is False
    # Problems: only the run a pipeline change fixes
    issues = c.get("/api/issues").json()["issues"]
    assert [i["source"]["ref"] for i in issues] == [ai_run]
    crash = next(r for r in body["reports"] if r.get("incident"))
    assert c.delete(f"/api/app-reports/{crash['id']}").json() == {"deleted": crash["id"]}
    assert inc.list() == []
    assert c.delete(f"/api/app-reports/{crash['id']}").status_code == 404


def test_the_chat_rule_is_in_the_defaults_and_added_once_to_old_files() -> None:
    from agentline import chat_instructions as ci

    assert ci.APP_PROBLEMS in ci.read().splitlines()  # a new data folder
    assert ci.FRESH_DESIGN in ci.read().splitlines()
    old = "- Be brief.\n\n## Lessons from failed runs\n- Check inputs.\n"
    ci.path().write_text(old)
    ci._upgrades_path().unlink()
    lines = ci.read().splitlines()
    assert lines == ["- Be brief.", ci.APP_PROBLEMS, ci.FRESH_DESIGN, "",
                     "## Lessons from failed runs", "- Check inputs."]  # fmt: skip
    ci.write(old)  # the user deletes it: it stays deleted
    assert ci.read() == old


def test_live_state_lists_app_reports() -> None:
    from agentline.assistant import live_text

    text = live_text(Live(triage=[
        {"id": "t0123456789ab", "incident": "abc", "verdict": "infrastructure",
         "summary": "Crash: SIGTRAP", "github": False},
        {"id": "t0123456789ac", "run": "r1", "node": "b", "verdict": "ai-instruction",
         "summary": "no steps", "github": False},
    ]))  # fmt: skip
    assert "APP REPORTS (agentline's own problems; not on Problems):" in text
    assert "t0123456789ab: crash report abc: Crash: SIGTRAP (not reported yet)" in text
    assert "t0123456789ac: run r1 node b: ai-instruction no steps" in text
