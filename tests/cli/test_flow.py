"""run -> wait -> approve -> worker, runs list/show, cancel/retry, audit."""

from __future__ import annotations

import json

import pytest

from agentline.cli import build_parser
from tests.cli.helpers import FLAKY, Cli

PAYLOAD = {"ticket": "PROJ-1", "branch": "agentline/r1", "sha": "abc1234"}


def test_run_wait_approve_deploy(cli: Cli) -> None:
    code, out, _ = cli.run(
        "run", "gated", "--payload", json.dumps(PAYLOAD), "--wait", "--timeout", "20"
    )
    assert code == 0, out
    run_id = out.split()[1]
    assert f"run {run_id}: waiting" in out
    assert f"next: agentline approve {run_id} --node approve" in out

    show = cli.ok("runs", "show", run_id)
    assert "approve#1" in show and "gate:     Deploy agentline/r1 (done plan#1)?" in show
    assert "lineage" in show and "wrote plan" in show

    out = cli.ok("approve", run_id, "--comment", "ship it")
    assert "approved approve#1" in out and "alice" in out
    cli.ok("worker", "--until-idle")

    v = cli.json("runs", "show", run_id)
    assert v["run"]["status"] == "done"
    deploy = next(x for x in v["executions"] if x["node_id"] == "deploy")
    assert deploy["status"] == "done" and deploy["detail"]["deploy"]["sha"] == "abc1234"
    actions = [(a["action"], a["actor"]) for a in v["audit"]]
    assert ("approve", "alice") in actions
    dep = next(a for a in v["audit"] if a["action"] == "deploy")
    assert dep["detail"]["approved_by"] == "alice"
    assert dep["detail"]["sha"] == "abc1234" and dep["detail"]["env"] == "eod"
    assert dep["detail"]["branch"] == "agentline/r1" and dep["detail"]["exit_code"] == 0
    assert dep["actor"].startswith("agentline:")
    assert v["next"] == []

    audit = cli.json("audit", "--run", run_id)
    assert [a["action"] for a in audit] == ["approve", "deploy"]
    text = cli.ok("audit")
    assert "approve" in text and "deploy" in text


def test_dedupe_and_list(cli: Cli) -> None:
    a = cli.submit("gated", PAYLOAD, "--dedupe-key", "jira:PROJ-1")
    out = cli.ok("run", "gated", "--payload", "{}", "--dedupe-key", "jira:PROJ-1")
    assert f"run {a} already exists" in out
    b = cli.submit("review")
    runs = cli.json("runs", "list")
    assert {r["id"] for r in runs} == {a, b}
    assert cli.json("runs", "list", "--status", "pending")[0]["id"] in (a, b)
    table = cli.ok("runs", "list")
    assert "jira:PROJ-1" in table and "PIPELINE" in table


def test_run_by_path_and_payload_file(cli: Cli) -> None:
    path = cli.root / "elsewhere.yaml"
    path.write_text((cli.pipelines / "review.yaml").read_text().replace("name: review", "name: x"))
    pf = cli.root / "payload.json"
    pf.write_text('{"a": 1}')
    out = cli.ok("run", str(path), "--payload-file", str(pf))
    assert "(x)" in out
    code, _, err = cli.run("run", "nope")
    assert code == 1 and "unknown flow 'nope'" in err


def test_reject_cancel_retry_are_audited(cli: Cli) -> None:
    _, out, _ = cli.run("run", "gated", "--payload", json.dumps(PAYLOAD), "--wait")
    run_id = out.split()[1]
    cli.ok("reject", run_id, "--comment", "not today")
    v = cli.json("runs", "show", run_id)
    assert v["run"]["status"] == "dead" and "rejected by alice" in v["run"]["error"]
    assert f"agentline runs retry {run_id}" in v["next"]
    assert "1 dead execution" in cli.ok("runs", "retry", run_id)
    cli.ok("runs", "cancel", run_id)
    v = cli.json("runs", "show", run_id)
    assert v["run"]["status"] == "cancelled"
    assert [a["action"] for a in v["audit"]] == ["reject", "retry", "cancel"]
    assert v["audit"][0]["detail"]["comment"] == "not today"


def test_approver_check_writes_no_audit(cli: Cli) -> None:
    cli.write_pipeline(
        "strict",
        "version: 1\nname: strict\nnodes:\n"
        "  g: { type: gate, prompt: ok?, approvers: [bob] }\nedges: []\n",
    )
    code, out, _ = cli.run("run", "strict", "--wait")
    run_id = out.split()[1]
    code, _, err = cli.run("approve", run_id)
    assert code == 1 and "alice is not an approver" in err
    assert cli.json("audit", "--run", run_id) == []


def test_wait_timeout_and_dead_exit_codes(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    cli.write_pipeline(
        "boom",
        "version: 1\nname: boom\nnodes:\n"
        "  a: { type: agent, runner: fake, prompt: 'mode:secretfail', out: x }\nedges: []\n",
    )
    monkeypatch.setenv("AGENTLINE_TEST_TOKEN", "x" * 12)
    code, out, _ = cli.run("run", "boom", "--wait", "--timeout", "20")
    assert code == 1 and ": dead" in out and "error: a#1: PermanentError" in out
    cli.write_pipeline("flaky", FLAKY)  # first attempt fails, the retry backs off ~1 s
    code, out, _ = cli.run("run", "flaky", "--wait", "--timeout", "0.3")
    assert code == 2 and "after 0.3s" in out


def test_global_options_after_subcommand(cli: Cli) -> None:
    args = build_parser().parse_args(["runs", "list", "--db", "/x.db", "--log-format", "json"])
    assert args.db == "/x.db" and args.log_format == "json"
    args = build_parser().parse_args(["--db", "/y.db", "runs", "list"])
    assert args.db == "/y.db" and args.log_format == "text"
    assert cli.db.exists() is False
    cli.ok("runs", "list")
    assert cli.db.exists()  # default: $AGENTLINE_HOME/agentline.db
