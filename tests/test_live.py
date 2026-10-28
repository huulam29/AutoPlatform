"""Live reload (agentline.live): settings changes reach runs already going; structure changes
apply to new runs only; a broken edit keeps the last good version."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentline import App, SqliteLedger
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.live import LivePipelines, current
from agentline.registry import RunRequest, RunResult
from agentline.runners.function import FunctionRunner

TOML = """\
[providers.p]
type = "openai-compatible"
base_url = "http://127.0.0.1:9/v1"

[models.m]
provider = "p"
model = "{model}"

[harnesses.h]
kind = "builtin"
model = "m"
"""
PIPE = """\
version: 1
name: flow
nodes:
  ask: {{ type: gate, prompt: "go?" }}
  work: {{ type: agent, harness: h, prompt: "do it" }}
{extra}edges:
  - ask -> work
{extra_edge}"""
EXTRA = '  extra: { type: agent, harness: h, prompt: "more" }\n'


@pytest.fixture
def world(tmp_path: Path) -> Any:
    calls: list[tuple[str, str]] = []

    async def fake(req: RunRequest) -> RunResult:
        calls.append((req.node_id, str(req.params.get("model"))))
        return RunResult(output={"ok": True})

    def registry(cfg: Config) -> Any:
        reg = default_registry(cfg)
        reg.add_runner(FunctionRunner("llm", fake))
        return reg

    (tmp_path / "flow").mkdir()
    toml = tmp_path / "agentline.toml"
    toml.write_text(TOML.format(model="a/old"))
    write(tmp_path, extra=False)
    app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0)
    live = LivePipelines(app, tmp_path, toml, registry_factory=registry)
    yield app, live, calls, tmp_path, toml
    app.ledger.close()


def write(root: Path, *, extra: bool, broken: bool = False) -> None:
    text = PIPE.format(
        extra=EXTRA if extra else "", extra_edge="  - work -> extra\n" if extra else ""
    )
    if broken:
        text = text.replace("harness: h", "harness: ghost")
    (root / "flow" / "pipeline.yaml").write_text(text)


def drain(app: App) -> None:
    asyncio.run(app.worker().run_until_idle())


def bump(path: Path) -> None:
    """Make sure the modification time moves on (fast file systems, same second)."""
    st = path.stat()
    import os

    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_a_settings_change_reaches_a_run_already_going(world: Any) -> None:
    app, live, calls, _, toml = world
    run = app.submit_sync("flow", {})
    drain(app)  # waits at the gate
    toml.write_text(TOML.format(model="b/new"))
    bump(toml)
    out = live.check()
    assert out is not None and out["updated"] == ["flow"] and out["problems"] == {}
    asyncio.run(app.approve(run.run_id, actor="t"))
    drain(app)
    assert calls == [("work", "b/new")]  # the running run used the new model


def test_a_structure_change_applies_to_new_runs_only(world: Any) -> None:
    app, live, calls, root, _ = world
    old = app.submit_sync("flow", {})
    drain(app)
    write(root, extra=True)
    bump(root / "flow" / "pipeline.yaml")
    assert live.check()["restructured"] == ["flow"]
    asyncio.run(app.approve(old.run_id, actor="t"))
    drain(app)
    assert [c[0] for c in calls] == ["work"]  # the old run kept its two nodes
    new = app.submit_sync("flow", {})
    drain(app)
    asyncio.run(app.approve(new.run_id, actor="t"))
    drain(app)
    assert [c[0] for c in calls] == ["work", "work", "extra"]
    # nothing runs on the old structure any more: it is forgotten at the next reload
    assert len(app._versions["flow"]) == 2
    live.reload("again")
    assert len(app._versions["flow"]) == 1


def test_a_broken_edit_keeps_the_last_good_version(world: Any) -> None:
    app, live, _, root, _ = world
    before = app.graph("flow")
    write(root, extra=False, broken=True)
    bump(root / "flow" / "pipeline.yaml")
    out = live.check()
    assert "kept the last good version" in out["problems"]["flow/pipeline.yaml"]
    assert app.graph("flow") is before and [p.name for p in current(live)] == ["flow"]


def test_new_and_deleted_pipelines(world: Any) -> None:
    app, live, _, root, _ = world
    (root / "other").mkdir()
    (root / "other" / "pipeline.yaml").write_text(
        'version: 1\nname: other\nnodes:\n  g: { type: gate, prompt: "ok?" }\n'
    )
    assert live.check()["added"] == ["other"]
    assert app.flow_names() == ["flow", "other"]
    run = app.submit_sync("other", {})
    drain(app)
    (root / "other" / "pipeline.yaml").unlink()
    assert live.check()["removed"] == ["other"]
    assert app.flow_names() == ["flow"]
    asyncio.run(app.approve(run.run_id, actor="t"))  # the run already going still finishes
    drain(app)
    assert app.get_run(run.run_id).status == "done"


def test_nothing_changed_means_no_reload(world: Any) -> None:
    _, live, _, _, _ = world
    v = live.version
    assert live.check() is None and live.version == v


def test_a_broken_config_keeps_the_previous_settings(world: Any) -> None:
    app, live, _, _, toml = world
    toml.write_text("[providers.p\n")
    bump(toml)
    out = live.check()
    assert "kept the previous settings" in out["problems"]["agentline.toml"]
    assert app.flow_names() == ["flow"]


def test_saves_through_the_api_reach_the_engine_without_a_restart(world: Any) -> None:
    from fastapi.testclient import TestClient

    from agentline import restart
    from agentline.api.server import create_server

    app, live, calls, root, _ = world
    c = TestClient(
        create_server(app, live, pipelines_dir=root, registry=live.registry, config=live.config)
    )
    run = app.submit_sync("flow", {})
    drain(app)
    # a settings save: the model of `m` changes, and the waiting run uses it
    r = c.put("/api/settings/models/m", json={"provider": "p", "model": "c/saved"})
    assert r.status_code == 200, r.text
    assert r.json()["note"] is None  # saves are silent: the engine already uses them
    assert c.get("/api/status").json()["restart_needed"] == []
    asyncio.run(app.approve(run.run_id, actor="t"))
    drain(app)
    assert calls == [("work", "c/saved")]
    # a new pipeline saved in the editor can be started right away
    src = 'version: 1\nname: fresh\nnodes:\n  g: { type: gate, prompt: "ok?" }\n'
    r = c.put("/api/pipelines/fresh/pipeline/source", json={"source": src})
    assert r.status_code == 200, r.text
    assert "fresh" in c.get("/api/status").json()["pipelines"]
    assert c.post("/api/runs", json={"pipeline": "fresh", "payload": {}}).status_code == 200
    assert restart.pending() == []


def test_a_key_changed_in_the_secrets_file_reaches_the_engine(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from agentline.secrets import secrets_path

    _, live, _, _, _ = world
    monkeypatch.delenv("FAKE_KEY", raising=False)
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("FAKE_KEY=one\n")  # the Mac app's Keys screen writes the file itself
    live.check()
    assert os.environ["FAKE_KEY"] == "one"
    path.write_text("FAKE_KEY=two\n")
    bump(path)
    live.check()
    assert os.environ["FAKE_KEY"] == "two"
    path.write_text("")
    bump(path)
    live.check()
    assert "FAKE_KEY" not in os.environ
    monkeypatch.setenv("FAKE_KEY", "from-the-shell")  # set outside the file: it wins
    path.write_text("FAKE_KEY=three\n")
    bump(path)
    live.check()
    assert os.environ["FAKE_KEY"] == "from-the-shell"
