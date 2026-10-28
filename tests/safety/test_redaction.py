"""A fake secret in the environment never reaches the SQLite file, artifact files, logs or
CLI output, even when an agent echoes it in its output, transcript, logs and errors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentline.redact import Redactor, is_secret_name
from tests.cli.helpers import Cli

SECRET = "sk-fake-9f8e7d6c5b4a3210"
MARK = "[REDACTED:AGENTLINE_TEST_TOKEN]"

LEAKY = """\
version: 1
name: leaky
nodes:
  talk:
    type: agent
    runner: fake
    prompt: "mode:secret"
    out: said
  gate:
    type: gate
    in: [said]
    prompt: "Agent said: {{said.summary}}"
  fail:
    type: agent
    runner: fake
    prompt: "mode:secretfail"
    out: never
edges:
  - talk -> gate -> fail
"""


def _all_bytes(root: Path) -> list[tuple[Path, bytes]]:
    return [(p, p.read_bytes()) for p in root.rglob("*") if p.is_file()]


def test_secret_never_reaches_ledger_files_logs_or_output(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTLINE_TEST_TOKEN", SECRET)
    cli.write_pipeline("leaky", LEAKY)
    payload = json.dumps({"note": f"payload has {SECRET}"})
    code, out1, err1 = cli.run(
        "--log-format", "json", "run", "leaky", "--payload", payload, "--wait", "--timeout", "20"
    )
    assert code == 0, out1 + err1
    run_id = out1.split()[1]
    code, out2, err2 = cli.run("--log-format", "json", "approve", run_id, "--comment", SECRET)
    assert code == 0
    code, out3, err3 = cli.run("--log-format", "json", "worker", "--until-idle")
    assert code == 0
    show = cli.ok("runs", "show", run_id)
    show_json = cli.ok("runs", "show", run_id, "--json")
    audit = cli.ok("audit", "--json")

    v = json.loads(show_json)
    assert v["run"]["status"] == "dead"
    fail = next(x for x in v["executions"] if x["node_id"] == "fail")
    assert fail["error"] == f"PermanentError: auth failed for token {MARK}"
    gate_audit = next(a for a in v["audit"] if a["action"] == "approve")
    assert gate_audit["detail"]["comment"] == MARK

    app = cli.app()
    said = app.result(run_id, "talk")
    assert said == {"summary": f"the token is {MARK}", "nested": [{"t": MARK}]}
    assert app.get_run(run_id).payload == {"note": f"payload has {MARK}"}  # type: ignore[union-attr]
    transcript = next(a for a in app.artifacts(run_id) if a["name"] == "transcript.first")
    assert MARK.encode() in app.artifact(transcript["hash"])["value"]  # type: ignore[index]
    app.ledger.close()

    # the redacted log line is there, the secret isn't
    logs = err1 + err2 + err3
    assert f"calling the API with token {MARK}" in logs
    for text in (out1, err1, out2, err2, out3, err3, show, show_json, audit):
        assert SECRET not in text
    files = _all_bytes(cli.root / "home")  # agentline.db, -wal, -shm, artifacts/
    assert any(p.name == "agentline.db" for p, _ in files)
    for path, data in files:
        assert SECRET.encode() not in data, f"secret found in {path}"


def test_redactor_rules() -> None:
    env = {
        "GITHUB_TOKEN": "ghp_aaaaaaaaaaaa",
        "MY_API_KEY": "key-bbbbbbbbbb",
        "DB_PASSWORD": "short",  # under 8 chars: ignored
        "WEBHOOK_SECRET": "whsec-cccccccc",
        "HOME": "/Users/someone-long",
        "ANTHROPIC_API_KEY": "sk-ant-dddddddd",
    }
    r = Redactor.from_env(env)
    assert r.names == ["ANTHROPIC_API_KEY", "GITHUB_TOKEN", "MY_API_KEY", "WEBHOOK_SECRET"]
    text = "a ghp_aaaaaaaaaaaa b key-bbbbbbbbbb short /Users/someone-long"
    assert r.redact(text) == (
        "a [REDACTED:GITHUB_TOKEN] b [REDACTED:MY_API_KEY] short /Users/someone-long"
    )
    assert r.redact_bytes(b"x whsec-cccccccc") == b"x [REDACTED:WEBHOOK_SECRET]"
    assert r.redact_value({"k": ["sk-ant-dddddddd", 1, None], "sk-ant-dddddddd": True}) == {
        "k": ["[REDACTED:ANTHROPIC_API_KEY]", 1, None],
        "[REDACTED:ANTHROPIC_API_KEY]": True,
    }
    # longest match wins when one secret contains another
    r2 = Redactor({"A_TOKEN": "abcdefgh", "B_TOKEN": "abcdefgh-ijkl"})
    assert r2.redact("abcdefgh-ijkl abcdefgh") == "[REDACTED:B_TOKEN] [REDACTED:A_TOKEN]"
    r2.add("C_KEY", "zzzzzzzzzz")
    assert r2.redact("zzzzzzzzzz") == "[REDACTED:C_KEY]"
    # known limit: a transformed secret is not caught
    import base64

    encoded = base64.b64encode(b"abcdefgh-ijkl").decode()
    assert r2.redact(encoded) == encoded
    empty = Redactor()
    value = {"a": "b"}
    assert empty.redact_value(value) is value and not empty
    assert is_secret_name("jira_api_token") and not is_secret_name("TOKENIZER")
