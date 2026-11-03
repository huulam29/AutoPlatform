"""agentline resolve: retry | skip | fail | done, unknown effects, audit rows."""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.helpers import FLAKY, Cli


def _review_run(cli: Cli) -> str:
    run_id = cli.submit("review")
    cli.ok("worker", "--until-idle")
    v = cli.json("runs", "show", run_id)
    assert v["run"]["status"] == "needs-review"
    assert v["executions"][0]["reason"] == "manual_check"
    assert any("--action retry|skip|fail|done" in n for n in v["next"])
    return run_id


def _execs(cli: Cli, run_id: str) -> dict[str, Any]:
    return {x["key"]: x for x in cli.json("runs", "show", run_id)["executions"]}


def test_resolve_done_with_result(cli: Cli) -> None:
    run_id = _review_run(cli)
    out = cli.ok("resolve", run_id, "--action", "done", "--result", '{"summary": "by hand"}')
    assert "resolved check#1" in out
    cli.ok("worker", "--until-idle")
    v = cli.json("runs", "show", run_id)
    assert v["run"]["status"] == "done"
    after = next(x for x in v["lineage"] if x["node"] == "after#1")
    assert after["reads"][0]["summary"] == '{summary: "by hand"}'
    entry = v["audit"][-1]
    assert entry["action"] == "resolve" and entry["actor"] == "alice"
    assert entry["detail"]["action"] == "done" and entry["detail"]["result"] == {
        "summary": "by hand"
    }
    assert entry["detail"]["reason"] == "manual_check" and entry["node_id"] == "check"


def test_resolve_skip_and_fail(cli: Cli) -> None:
    run_id = _review_run(cli)
    cli.ok("resolve", run_id, "--node", "check", "--action", "skip")
    ex = _execs(cli, run_id)
    assert ex["check#1"]["status"] == "skipped" and ex["after#1"]["status"] == "skipped"

    run_id = _review_run(cli)
    cli.ok("resolve", run_id, "--action", "fail")
    v = cli.json("runs", "show", run_id)
    assert v["run"]["status"] == "dead"
    assert [a["detail"]["action"] for a in v["audit"]] == ["fail"]


def test_resolve_retry_and_errors(cli: Cli) -> None:
    run_id = _review_run(cli)
    code, _, err = cli.run("resolve", run_id, "--node", "after", "--action", "retry")
    assert code == 1 and "no execution in needs-review at node after" in err
    code, _, err = cli.run("resolve", run_id, "--action", "done", "--result", "{nope")
    assert code == 1 and "--result is not valid JSON" in err
    code, _, err = cli.run("resolve", "r-missing", "--action", "retry")
    assert code == 1 and "no run r-missing" in err
    cli.ok("resolve", run_id, "--action", "retry")
    assert _execs(cli, run_id)["check#1"]["status"] == "ready"
    with pytest.raises(SystemExit):
        cli.run("resolve", run_id, "--action", "explode")


def test_unknown_effect_resolved_as_done(cli: Cli) -> None:
    cli.write_pipeline("flaky", FLAKY)
    run_id = cli.submit("flaky")
    cli.ok("worker", "--until-idle")  # attempt 1 fails in the call; attempt 2: no advisor
    v = cli.json("runs", "show", run_id)
    ex = v["executions"][0]
    assert ex["status"] == "needs-review" and ex["reason"] == "effect_unknown"
    assert ex["detail"]["effect"] == "create_thing"
    assert v["effects"][0]["status"] == "failed"  # the call raised (decision 78)
    assert "--effect redo|done" in v["next"][0]
    code, _, err = cli.run("resolve", run_id, "--action", "retry", "--effect-result", "{}")
    assert code == 1 and "--effect-result goes with --effect done" in err
    cli.ok(
        "resolve", run_id, "--action", "retry", "--effect", "done", "--effect-result", '{"id": 7}'
    )
    cli.ok("worker", "--until-idle")
    v = cli.json("runs", "show", run_id)
    assert v["run"]["status"] == "done"
    assert v["effects"][0]["status"] == "committed"
    assert v["lineage"][0]["writes"][0]["summary"] == "{id: 7}"
    assert v["audit"][-1]["detail"]["effect_decision"] == "done"
