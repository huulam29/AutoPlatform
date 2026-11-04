"""serve's data folder lock, start marker and in-place restart (spec 006 US1, US5, US7)."""

from __future__ import annotations

import json
import os
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentline import runmarker
from agentline.cli import commands
from agentline.enginelock import EngineLock
from agentline.incidents import IncidentStore
from tests.cli.helpers import Cli

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the engine needs POSIX")


def fake_uvicorn(
    monkeypatch: pytest.MonkeyPatch, during: Callable[[Any], None] | None = None
) -> None:
    class Config:
        def __init__(self, app: Any, **kw: Any) -> None:
            self.app, self.kw = app, kw

    class Server:
        def __init__(self, config: Config) -> None:
            self.config = config
            self.should_exit = False

        async def serve(self) -> None:
            if during is not None:
                during(self.config.app)

    fake = types.ModuleType("uvicorn")
    fake.Config, fake.Server = Config, Server  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake)


def home() -> Path:
    h = Path(os.environ["AGENTLINE_HOME"])
    h.mkdir(parents=True, exist_ok=True)
    return h


def test_a_second_engine_on_the_same_data_folder_exits(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_uvicorn(monkeypatch)
    first = EngineLock(home())
    first.acquire("http://127.0.0.1:8080")
    try:
        code, _, err = cli.run("serve", "--port", "18998")
    finally:
        first.release()
    assert code == 1
    assert "another engine is running on this data folder" in err
    assert "http://127.0.0.1:8080" in err and f"pid {os.getpid()}" in err


def test_the_lock_and_marker_are_held_while_serving_and_gone_after(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def during(app: Any) -> None:
        seen["marker"] = json.loads((home() / "engine.run.json").read_text())
        seen["address"] = app.state.address
        other = EngineLock(home())
        try:
            other.acquire("x")
            seen["second"] = "got it"
        except Exception as e:
            seen["second"] = type(e).__name__

    fake_uvicorn(monkeypatch, during)
    cli.ok("serve", "--port", "18997")
    assert seen["second"] == "EngineLocked"
    assert seen["address"] == "http://127.0.0.1:18997"
    assert seen["marker"]["pid"] == os.getpid() and seen["marker"]["address"] == seen["address"]
    assert not (home() / "engine.run.json").exists()  # a clean stop
    EngineLock(home()).acquire("free again")  # released


def test_a_leftover_marker_becomes_one_unclean_exit_report(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_uvicorn(monkeypatch)
    (home() / "engine.run.json").write_text(
        json.dumps(
            {"pid": 4242, "started_at": 1.0, "version": "0.0", "platform": "x", "address": "a"}
        )
    )
    cli.ok("serve", "--port", "18996")
    reports = [i for i in IncidentStore().list() if i["kind"] == runmarker.KIND]
    assert len(reports) == 1 and reports[0]["status"] == "new"
    full = IncidentStore().get(reports[0]["id"]) or {}
    assert full["context"]["previous_pid"] == 4242
    cli.ok("serve", "--port", "18996")  # that start stopped cleanly: nothing new
    assert len([i for i in IncidentStore().list() if i["kind"] == runmarker.KIND]) == 1


def test_restart_runs_the_same_command_again(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def during(app: Any) -> None:
        app.state.restart = True  # what POST /api/engine/restart sets, with the stop event

    fake_uvicorn(monkeypatch, during)
    monkeypatch.setattr(sys, "argv", ["agentline", "serve", "--port", "18995"])
    monkeypatch.setattr(commands.os, "execv", lambda exe, argv: calls.append([exe, *argv]))
    cli.ok("serve", "--port", "18995")
    assert calls == [
        [sys.executable, sys.executable, "-m", "agentline.cli", "serve", "--port", "18995"]
    ]
    assert not (home() / "engine.run.json").exists()  # stopped cleanly before the exec


def test_inside_the_mac_app_no_marker_is_kept(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, bool] = {}
    monkeypatch.setenv("AGENTLINE_PARENT_PID", str(os.getpid()))
    fake_uvicorn(monkeypatch, lambda app: seen.update(marker=(home() / "engine.run.json").exists()))
    (home() / "engine.run.json").write_text("{}")  # left by an engine before the app took over
    cli.ok("serve", "--port", "18994")
    assert seen == {"marker": True}  # the app's own checks own unclean exits: not touched
    assert [i for i in IncidentStore().list() if i["kind"] == runmarker.KIND] == []
