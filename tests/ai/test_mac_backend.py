"""Engine support for the Mac app: approvals list, approve/reject and start-run endpoints,
status, Keychain-managed keys, and Jira polling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api.server import create_server
from agentline.builtins import default_registry
from agentline.pipeline import build_pipeline, parse_spec
from agentline.sources.jira_poll import assignment, build_jql, poll_once, run_jira_poll

GATED = """\
name: gated
trigger: { type: manual }
nodes:
  ok: { type: gate, prompt: "Ship {{payload.issue_key}}?" }
"""

POLLED = """\
name: polled
trigger: { type: poll, source: jira, event: issue_assigned, every: 1m, filter: { project: PROJ } }
nodes:
  ok: { type: gate, prompt: "Work on {{payload.issue_key}}?" }
"""


def make(tmp_path: Path, source: str = GATED) -> tuple[App, TestClient]:
    app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0)
    p = build_pipeline(parse_spec(source), default_registry())
    app.register_pipeline(p)
    (tmp_path / "pipelines").mkdir(exist_ok=True)
    server = create_server(app, [p], pipelines_dir=tmp_path / "pipelines")
    return app, TestClient(server)


def drain(app: App) -> None:
    asyncio.run(app.worker(concurrency=1).run_until_idle())


def test_start_run_approvals_and_approve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_USER", "Huu")
    app, c = make(tmp_path)
    r = c.post(
        "/api/runs",
        json={"pipeline": "gated", "payload": {"issue_key": "PROJ-1"}, "dedupe_key": "k1"},
    )
    assert r.status_code == 200 and r.json()["created"] is True
    again = c.post(
        "/api/runs", json={"pipeline": "gated", "payload": {}, "dedupe_key": "k1"}
    ).json()
    assert again == {"run_id": r.json()["run_id"], "created": False}
    assert c.post("/api/runs", json={"pipeline": "nope"}).status_code == 404
    drain(app)
    (gate,) = c.get("/api/approvals").json()
    assert gate["pipeline"] == "gated" and gate["prompt"] == "Ship PROJ-1?"
    assert gate["payload"] == {"issue_key": "PROJ-1"}
    ok = c.post(f"/api/runs/{gate['run_id']}/approve", json={"comment": "from my Mac"})
    assert ok.status_code == 200 and ok.json() == {
        "run_id": gate["run_id"],
        "node_id": "ok",
        "actor": "Huu",
    }
    assert c.post(f"/api/runs/{gate['run_id']}/approve", json={}).status_code == 409
    assert c.post("/api/runs/r-missing/approve", json={}).status_code == 404
    drain(app)
    assert app.get_run(gate["run_id"]).status == "done"  # type: ignore[union-attr]
    (row,) = [a for a in app.audit_log(gate["run_id"]) if a.action == "approve"]
    assert (
        row.actor == "Huu" and row.detail["via"] == "app" and row.detail["comment"] == "from my Mac"
    )


def test_reject_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_KEYS_MANAGED", "keychain")
    app, c = make(tmp_path)
    body = {"pipeline": "gated", "payload": {"issue_key": "PROJ-2"}}
    run_id = c.post("/api/runs", json=body).json()["run_id"]
    drain(app)
    assert c.post(f"/api/runs/{run_id}/reject", json={"comment": "no"}).status_code == 200
    assert app.get_run(run_id).status == "dead"  # type: ignore[union-attr]
    s = c.get("/api/status").json()
    assert s["pipelines"] == ["gated"] and s["keys_managed"] == "keychain"
    assert s["jira_poll"] == {"enabled": False}


def test_keys_can_be_set_in_the_mac_app_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """With the Mac app (keys_managed=keychain) the web Keys tab still works: the value goes to
    the secrets file, which the engine loads (the app's Keychain value wins if both exist)."""
    monkeypatch.setenv("AGENTLINE_KEYS_MANAGED", "keychain")
    _, c = make(tmp_path)
    r = c.put("/api/secrets/OPENROUTER_API_KEY", json={"value": "sk-or-123456789"})
    assert r.status_code == 200 and "sk-or-123456789" not in r.text
    assert "OPENROUTER_API_KEY=sk-or-123456789" in (isolated_home / "secrets.env").read_text()
    assert c.get("/api/settings").json()["keys_managed"] == "keychain"


def test_builtin_entries_cannot_be_deleted_but_overrides_can(
    tmp_path: Path, isolated_home: Path
) -> None:
    _, c = make(tmp_path)
    r = c.delete("/api/settings/models/free")
    assert r.status_code == 409 and "built in" in r.text
    assert (
        c.put(
            "/api/settings/models/free", json={"provider": "openrouter", "model": "x/y:free"}
        ).status_code
        == 200
    )
    assert c.delete("/api/settings/models/free").status_code == 200  # back to the built-in
    free = {m["name"]: m for m in c.get("/api/settings").json()["models"]}["free"]
    assert free["model"] == "openrouter/free" and free["implicit"] is True


def test_web_dist_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<title>bundled</title>")
    monkeypatch.setenv("AGENTLINE_WEB_DIST", str(web))
    _, c = make(tmp_path)
    assert "bundled" in c.get("/").text


# -- Jira polling ---------------------------------------------------------------------------------
ME = "acc-me"


def issue(key: str, history: list[tuple[str, str, str]]) -> dict[str, Any]:
    return {
        "key": key,
        "changelog": {
            "histories": [
                {"id": hid, "created": created, "items": [{"field": "assignee", "to": to}]}
                for hid, created, to in history
            ]
        },
    }


class FakeJira:
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        self.queries: list[str] = []

    def myself(self) -> dict[str, Any]:
        return {"accountId": ME}

    def search(self, jql: str) -> list[dict[str, Any]]:
        self.queries.append(jql)
        return self.issues


def test_assignment_picks_latest_change_to_me() -> None:
    i = issue(
        "PROJ-1",
        [
            ("10", "2026-10-19T09:00:00.000+0000", ME),
            ("11", "2026-10-19T10:00:00.000+0000", "someone-else"),
            ("12", "2026-10-19T13:00:00.000+0200", ME),  # 11:00 UTC, the latest
        ],
    )
    hid, _ = assignment(i, ME) or ("", 0)
    assert hid == "12"
    assert assignment(issue("X", []), ME) is None


def test_poll_only_starts_assignments_after_enable_and_dedupes(tmp_path: Path) -> None:
    app, _ = make(tmp_path, POLLED)
    p = build_pipeline(parse_spec(POLLED), default_registry())
    since = 1_792_400_000.0  # 2026-10-18T...
    jira = FakeJira(
        [
            issue("PROJ-1", [("100", "2026-10-19T09:00:00.000+0000", ME)]),  # after: start
            issue("PROJ-2", [("101", "2020-01-01T09:00:00.000+0000", ME)]),  # backlog: skip
            issue("PROJ-3", [("102", "2026-10-19T09:00:00.000+0000", "other")]),  # not me
        ]
    )
    first = asyncio.run(poll_once(app, p, jira, account=ME, since=since))
    second = asyncio.run(poll_once(app, p, jira, account=ME, since=since))
    assert len(first) == 1 and second == []
    run = app.get_run(first[0])
    assert run is not None and run.payload == {
        "issue_key": "PROJ-1",
        "assignee": ME,
        "event": "issue_assigned",
    }
    assert run.dedupe_key == f"jira:PROJ-1:assigned:{ME}:100"  # same key as the webhook path
    assert 'project = "PROJ"' in jira.queries[0] and "CHANGED TO currentUser()" in jira.queries[0]


def test_custom_jql_and_loop_status(tmp_path: Path) -> None:
    src = POLLED.replace("filter: { project: PROJ }", 'filter: { jql: "labels = agentline" }')
    p = build_pipeline(parse_spec(src), default_registry())
    assert build_jql(p.spec.trigger, 0) == "labels = agentline"
    app, _ = make(tmp_path, src)
    jira = FakeJira([issue("PROJ-9", [("200", "2099-01-01T00:00:00.000+0000", ME)])])

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_jira_poll(
                app, [p], stop, client_factory=lambda: jira, state_file=tmp_path / "s.json"
            )
        )
        await asyncio.sleep(0.3)
        stop.set()
        await task

    asyncio.run(go())
    status = app.runtime["jira_poll"]
    assert status["enabled"] and status["pipelines"] == ["polled"] and status["every_s"] == 60
    assert status["runs_started"] == 1 and status["last_error"] is None
    assert "polled" in (tmp_path / "s.json").read_text()


def test_poll_loop_reports_errors(tmp_path: Path) -> None:
    p = build_pipeline(parse_spec(POLLED), default_registry())
    app, _ = make(tmp_path, POLLED)

    def broken() -> Any:
        raise RuntimeError("JIRA_EMAIL and JIRA_API_TOKEN must be set to call Jira")

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_jira_poll(app, [p], stop, client_factory=broken, state_file=tmp_path / "s.json")
        )
        await asyncio.sleep(0.2)
        stop.set()
        await task

    asyncio.run(go())
    assert "JIRA_API_TOKEN" in app.runtime["jira_poll"]["last_error"]


def test_poll_trigger_spec_errors() -> None:
    from agentline.pipeline import SpecError

    with pytest.raises(SpecError, match="poll at most every 30s"):
        parse_spec(POLLED.replace("every: 1m", "every: 5s"))
    with pytest.raises(SpecError, match="poll triggers support source: jira"):
        parse_spec(POLLED.replace("source: jira", "source: github"))
    with pytest.raises(SpecError, match="'every' only applies to poll triggers"):
        parse_spec(GATED.replace("type: manual", "type: manual, every: 1m"))
