"""Run view API over a real run of the example pipeline (fake runners/actions)."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api import create_server
from agentline.api.runs import artifact_payload
from agentline.pipeline import load_pipeline

from ..pipeline.fakes import World, make_registry
from ..pipeline.test_spec import EXAMPLE


@pytest.fixture
def app(tmp_path: Path) -> Iterator[App]:
    ledger = SqliteLedger(tmp_path / "ledger.db")
    app = App(ledger, backoff_base=0.0, artifact_root=tmp_path / "artifacts")
    app.register_pipeline(load_pipeline(EXAMPLE, make_registry(World())))
    yield app
    ledger.close()


@pytest.fixture
def client(app: App, tmp_path: Path) -> Iterator[TestClient]:
    pdir = tmp_path / "pipelines"
    shutil.copytree(EXAMPLE.parent, pdir / "ticket-to-eod")
    with TestClient(create_server(app, [], pipelines_dir=pdir, token="")) as c:
        yield c


def drain(app: App) -> None:
    asyncio.run(app.worker(concurrency=3).run_until_idle())


def to_gate(app: App) -> str:
    sub = app.submit_sync("ticket-to-eod", {"issue_key": "PROJ-1"})
    drain(app)
    for state in ("changes_requested", "approved"):
        (key,) = [x.correlation_key for x in app.executions(sub.run_id) if x.status == "waiting"]
        asyncio.run(app.deliver_event(key or "", {"state": state, "comments": ["c"]}))
        drain(app)
    return sub.run_id


def test_run_waiting_at_the_gate(app: App, client: TestClient) -> None:
    run_id = to_gate(app)
    runs = client.get("/api/runs").json()["runs"]
    assert [r["id"] for r in runs] == [run_id]
    assert runs[0]["status"] == "waiting" and runs[0]["terminal"] is False
    assert client.get("/api/runs?status=done").json()["runs"] == []

    body = client.get(f"/api/runs/{run_id}").json()
    assert body["run"]["flow"] == "ticket-to-eod"
    assert body["pipeline"]["name"] == "ticket-to-eod/pipeline"
    assert len(body["pipeline"]["model"]["nodes"]) == 9
    keys = {x["key"]: x for x in body["executions"]}
    assert keys["implement#2"]["status"] == "done" and keys["approve#2"]["status"] == "waiting"
    assert keys["plan#1"]["cost_usd"] == pytest.approx(0.01)
    cmds = [c["command"] for c in body["next_commands"]]
    assert f"agentline approve {run_id}" in cmds and f"agentline reject {run_id}" in cmds
    lin = {e["node"]: e for e in body["lineage"]}
    assert {r["name"] for r in lin["implement#2"]["reads"]} == {"ticket", "plan", "review"}
    assert body["effects"] and all("status" in e for e in body["effects"])


def test_finished_run_and_artifacts(app: App, client: TestClient) -> None:
    run_id = to_gate(app)
    asyncio.run(app.approve(run_id, actor="huu"))
    drain(app)
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["run"]["status"] == "done" and body["run"]["terminal"] is True
    assert [c["command"] for c in body["next_commands"]] == [f"agentline runs show {run_id}"]

    plan = next(x for x in body["executions"] if x["key"] == "plan#1")
    art = client.get(f"/api/artifacts/{plan['result_ref']}").json()
    assert art["encoding"] == "json" and art["value"]["summary"] == "Fix login"
    assert art["truncated"] is False

    ref = plan["detail"]["attachments"]["transcript.first"]["ref"]
    att = client.get(f"/api/artifacts/{ref}").json()
    assert (att["encoding"], att["value"], att["media_type"]) == ("text", "planned", "text/plain")

    assert client.get("/api/artifacts/" + "0" * 64).status_code == 404
    assert client.get("/api/artifacts/not-a-hash").status_code == 400
    assert client.get("/api/runs/nope").status_code == 404


def test_large_artifacts_are_truncated(app: App) -> None:
    store = app.scheduler.artifacts
    with app.ledger.tx() as tx:
        text = store.put_bytes(tx, b"x" * 3000, "text/plain")
        blob = store.put_bytes(tx, bytes([0xFF, 0xFE]) * 1000, "application/octet-stream")
        big_json = store.put_json(tx, {"k": "v" * 3000})
    out = artifact_payload(app, text.hash, cap=1000)
    assert out is not None and out["truncated"] and out["value"] == "x" * 1000
    out = artifact_payload(app, blob.hash, cap=100)
    assert out is not None and out["encoding"] == "base64" and out["truncated"]
    out = artifact_payload(app, big_json.hash, cap=100)
    assert out is not None and out["encoding"] == "text" and len(out["value"]) == 100


def test_logs_returns_the_recent_redacted_lines(client: TestClient, monkeypatch) -> None:
    import io
    import logging

    from agentline.logs import RING, setup_logging

    monkeypatch.setenv("SOME_API_KEY", "sk-very-secret-value-123")
    RING.lines.clear()
    setup_logging("text", "INFO", stream=io.StringIO())
    logging.getLogger("agentline.test").info("hello sk-very-secret-value-123")
    lines = client.get("/api/logs", params={"limit": 50}).json()["lines"]
    assert any("hello" in line for line in lines)
    assert not any("sk-very-secret-value-123" in line for line in lines)
