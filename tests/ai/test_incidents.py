"""Crash and failure reports: store, redaction, worker/API/CLI recording."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger, StepContext
from agentline.api.server import create_server
from agentline.incidents import IncidentStore, fingerprint, normalize
from agentline.redact import Redactor

SECRET = "sk-or-very-secret-value-123456"


def boom(n: int) -> None:
    {}["missing-" + str(n)]  # a KeyError on purpose


def store(tmp_path: Path) -> IncidentStore:
    return IncidentStore(tmp_path / "incidents", redactor=Redactor({"OPENROUTER_API_KEY": SECRET}))


def test_same_problem_merges_into_one_record(tmp_path: Path) -> None:
    s = store(tmp_path)
    for n in (1, 2, 3):  # different ids in the message, same bug
        try:
            boom(n)
        except KeyError as e:
            rec = s.record_exception("worker_error", e, context={"run_id": f"r{n}"})
    items = s.list()
    assert len(items) == 1 and items[0]["count"] == 3
    assert items[0]["title"].startswith("KeyError") and "boom" in items[0]["title"]
    assert "traceback" not in items[0]  # summaries only
    full = s.get(rec["id"])
    assert full is not None and full["context"] == {"run_id": "r3"} and "boom" in full["traceback"]
    assert full["first_seen"] <= full["last_seen"] and full["status"] == "new"
    s.mark_seen(rec["id"])
    assert s.new_count() == 0
    s.record("worker_error", title="x", exception_type="KeyError", message="'missing-9'",
             frames=[f"{Path(__file__).name}:boom"])  # fmt: skip
    assert s.new_count() in (0, 1)  # a different fingerprint unless frames match exactly


def test_secrets_never_written(tmp_path: Path) -> None:
    s = store(tmp_path)
    try:
        raise RuntimeError(f"auth failed with key {SECRET}")
    except RuntimeError as e:
        s.record_exception("engine_exception", e, context={"header": f"Bearer {SECRET}"})
    raw = "".join(p.read_text() for p in (tmp_path / "incidents").glob("*.json"))
    assert SECRET not in raw and "REDACTED" in raw


def test_fingerprint_ignores_ids_and_numbers() -> None:
    a = fingerprint("engine", "api_error", "KeyError", "run r01a1532d913947e6f448 at 12:00", [])
    b = fingerprint("engine", "api_error", "KeyError", "run r01a15529fadb81122b50 at 13:05", [])
    assert a == b
    assert normalize("0xdeadbeef failed 3 times") == "<hex> failed N times"


def test_delete_and_clear(tmp_path: Path) -> None:
    s = store(tmp_path)
    r1 = s.record("api_error", title="one", message="a")
    s.record("api_error", title="two", message="b")
    (s.root / r1["id"]).mkdir()
    (s.root / r1["id"] / "crash.ips").write_text("{}")
    assert s.delete(r1["id"]) and not (s.root / r1["id"]).exists()
    assert not s.delete("not-an-id") and s.get("../etc/passwd") is None
    assert s.clear() == 1 and s.list() == []


def make_app(tmp_path: Path) -> App:
    return App(
        SqliteLedger(tmp_path / "l.db"), backoff_base=0, incidents=IncidentStore(tmp_path / "inc")
    )


def test_bug_in_a_step_is_recorded_but_ordinary_failures_are_not(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    flow = app.flow("t")

    @flow.step("buggy", max_attempts=1)
    async def buggy(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"x": inputs["nope"]}  # KeyError: a bug

    @flow.step("flaky", max_attempts=1)
    async def flaky(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("the remote API said 503")  # an expected kind of failure

    sub = app.submit_sync("t", {})
    asyncio.run(app.worker(concurrency=1).run_until_idle())
    items = app.incidents.list()
    assert [i["kind"] for i in items] == ["worker_error"]
    full = app.incidents.get(items[0]["id"])
    assert full["exception_type"] == "KeyError"
    assert full["context"]["run_id"] == sub.run_id and full["context"]["node"] == "buggy#1"


def test_worker_loop_survives_ledger_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    flow = app.flow("t")

    @flow.step("a")
    async def a(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {}

    app.submit_sync("t", {})
    worker = app.worker(concurrency=1)
    real = worker._claim
    calls = {"n": 0}

    def flaky_claim() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise AttributeError("ledger hiccup")
        return real()

    monkeypatch.setattr(worker, "_claim", flaky_claim)
    asyncio.run(worker.run_until_idle())  # didn't crash; finished the run
    assert [i["kind"] for i in app.incidents.list()] == ["worker_error"]
    assert app.incidents.get(app.incidents.list()[0]["id"])["context"]["where"] == "worker loop"


def test_api_500_becomes_an_incident_and_api_lists_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    (tmp_path / "pipelines").mkdir()
    server = create_server(app, [], pipelines_dir=tmp_path / "pipelines")

    def explode() -> list[str]:
        raise ZeroDivisionError("division by zero in a handler")

    monkeypatch.setattr(app, "flow_names", explode)  # /api/status calls it
    c = TestClient(server, raise_server_exceptions=False)
    r = c.get("/api/status")
    assert r.status_code == 500 and r.json()["incident"]
    listed = c.get("/api/incidents").json()
    assert listed["new"] == 1 and listed["incidents"][0]["kind"] == "api_error"
    iid = listed["incidents"][0]["id"]
    full = c.get(f"/api/incidents/{iid}").json()
    assert full["context"] == {"method": "GET", "path": "/api/status"}
    assert c.post(f"/api/incidents/{iid}/seen").status_code == 200
    assert c.get("/api/incidents").json()["new"] == 0
    assert c.delete(f"/api/incidents/{iid}").status_code == 200
    assert c.get(f"/api/incidents/{iid}").status_code == 404


def test_cli_list_show_export(
    isolated_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agentline.cli import main

    s = IncidentStore()  # the default dir under AGENTLINE_HOME, as the CLI uses
    rec = s.record("engine_exit", title="engine exited with code 1", message="boom",
                   context={"exit_code": 1})  # fmt: skip
    assert main(["incidents", "list"]) == 0
    assert "engine exited with code 1" in capsys.readouterr().out
    assert main(["incidents", "show", rec["id"]]) == 0
    assert '"exit_code": 1' in capsys.readouterr().out
    assert main(["incidents", "export", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / f"{rec['id']}.json").exists()
    assert "engine exited" in (tmp_path / "out" / "summary.md").read_text()
    assert main(["incidents", "show", "0000000000000000"]) == 1


def test_process_hook_records_unhandled_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentline.incidents import install_process_hooks

    s = IncidentStore(tmp_path / "inc")
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    install_process_hooks(s)
    try:
        raise TypeError("crash in main")
    except TypeError as e:
        sys.excepthook(type(e), e, e.__traceback__)
    (only,) = s.list()
    assert only["kind"] == "engine_exception" and "crash in main" in only["title"]
    assert (
        json.loads((s.root / f"{only['id']}.json").read_text())["context"]["where"] == "main thread"
    )
