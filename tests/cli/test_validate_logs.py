"""agentline validate, and the structured log formats."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from agentline.logs import setup_logging
from agentline.redact import Redactor
from tests.cli.helpers import Cli

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "ticket-to-eod" / "pipeline.yaml"

UNGATED = """\
version: 1
name: ungated
nodes:
  build:
    type: agent
    runner: fake
    prompt: build
    out: branch
  deploy:
    type: action
    uses: env.deploy
    with: { env: eod }
    in: [branch]
edges:
  - build -> deploy
"""


def test_validate_ok_and_example(cli: Cli) -> None:
    out = cli.ok("validate", str(cli.pipelines / "gated.yaml"), str(EXAMPLE))
    assert "gated.yaml: ok (gated, 3 nodes)" in out and "ticket-to-eod, 9 nodes" in out


def test_validate_deploy_without_gate(cli: Cli) -> None:
    path = cli.root / "ungated.yaml"
    path.write_text(UNGATED)
    code, out, _ = cli.run("validate", str(path))
    assert code == 1
    assert f"{path}:9:1: nodes.deploy: deploy-class action 'env.deploy' can run without" in out
    assert "(hint: put a gate node on every path before it)" in out


def test_validate_reports_every_issue_with_position(cli: Cli) -> None:
    path = cli.root / "bad.yaml"
    path.write_text(
        "version: 1\nname: bad\nnodes:\n"
        "  a: { type: agent, runner: nope, prompt: hi, out: x, typo: 1 }\n"
        "edges: []\n"
    )
    code, out, _ = cli.run("validate", "-q", str(path), str(cli.pipelines / "gated.yaml"))
    assert code == 1
    lines = out.strip().splitlines()
    assert any(f"{path}:4:" in ln and "nodes.a.typo" in ln for ln in lines)
    assert all(ln.startswith(f"{path}:") for ln in lines)  # -q: no "ok" line for gated
    code, out, _ = cli.run("validate", str(cli.root / "missing.yaml"))
    assert code == 1 and "No such file" in out


def test_invalid_pipeline_in_dir_is_skipped_with_warning(cli: Cli) -> None:
    cli.write_pipeline("ungated", UNGATED)
    code, _out, err = cli.run("runs", "list")
    assert code == 0 and "skipping invalid pipeline" in err and "ungated.yaml" in err


def test_json_logs_carry_run_context(cli: Cli) -> None:
    code, out, err = cli.run("--log-format", "json", "run", "review", "--wait", "--timeout", "20")
    assert code == 0
    run_id = out.split()[1]
    records = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    assert records, err
    assert all({"ts", "level", "logger", "msg"} <= r.keys() for r in records)
    review = [r for r in records if "needs review" in r["msg"]]
    assert review[0]["run_id"] == run_id and review[0]["node_id"] == "check"
    assert review[0]["iteration"] == 1 and review[0]["level"] == "WARNING"
    assert review[0]["execution_id"].startswith("x")


def test_text_logs_and_redaction_filter() -> None:
    buf = io.StringIO()
    handler = setup_logging(
        "text", "DEBUG", redactor=Redactor({"X_TOKEN": "tok-12345678"}), stream=buf
    )
    try:
        log = logging.getLogger("agentline.test")
        log.info("hello %s", "tok-12345678", extra={"run_id": "r1", "node_id": "n", "iteration": 2})
        try:
            raise ValueError("bad tok-12345678")
        except ValueError:
            log.exception("boom")
        lines = buf.getvalue()
        assert "hello [REDACTED:X_TOKEN] [r1 n#2]" in lines
        assert "tok-12345678" not in lines and "ValueError: bad [REDACTED:X_TOKEN]" in lines
    finally:
        logging.getLogger().removeHandler(handler)

    buf = io.StringIO()
    handler = setup_logging("json", "INFO", redactor=Redactor(), stream=buf)
    try:
        logging.getLogger("agentline.test").warning("plain")
        rec = json.loads(buf.getvalue())
        assert rec["msg"] == "plain" and rec["level"] == "WARNING" and "run_id" not in rec
    finally:
        logging.getLogger().removeHandler(handler)


def test_access_log_drops_successful_polls_but_keeps_errors_and_writes(monkeypatch) -> None:
    import io
    import logging

    from agentline.logs import setup_logging

    monkeypatch.delenv("AGENTLINE_ACCESS_LOG", raising=False)
    out = io.StringIO()
    setup_logging("text", "INFO", stream=out)
    access = logging.getLogger("uvicorn.access")
    fmt = '%s - "%s %s HTTP/%s" %d'
    access.info(fmt, "127.0.0.1:1", "GET", "/api/runs?limit=200", "1.1", 200)
    access.info(fmt, "127.0.0.1:1", "HEAD", "/api/status", "1.1", 304)
    access.info(fmt, "127.0.0.1:1", "GET", "/api/runs/x", "1.1", 404)
    access.info(fmt, "127.0.0.1:1", "POST", "/api/runs", "1.1", 200)
    access.info(fmt, "127.0.0.1:1", "GET", "/api/settings", "1.1", 500)
    text = out.getvalue()
    assert "/api/runs?limit=200" not in text and "/api/status" not in text
    assert "/api/runs/x" in text and '"POST /api/runs' in text and "/api/settings" in text

    monkeypatch.setenv("AGENTLINE_ACCESS_LOG", "all")
    access.info(fmt, "127.0.0.1:1", "GET", "/api/approvals", "1.1", 200)
    assert "/api/approvals" in out.getvalue()
