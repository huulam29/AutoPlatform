"""Show in folder (agentline.reveal) and the start marker (agentline.runmarker)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentline import reveal, runmarker
from agentline.incidents import IncidentStore
from agentline.redact import Redactor


def test_allowed_only_inside_the_roots(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "sub").mkdir(parents=True)
    assert reveal.allowed(root, [root])
    assert reveal.allowed(root / "sub", [root])
    assert not reveal.allowed(root / ".." / "elsewhere", [root])
    assert not reveal.allowed(tmp_path, [root])


def test_a_symlink_out_of_the_roots_is_refused(tmp_path: Path) -> None:
    root, outside = tmp_path / "data", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks here")
    assert not reveal.allowed(root / "link", [root])


def test_the_file_manager_command_per_platform(tmp_path: Path) -> None:
    f = tmp_path / "a.json"
    f.write_text("{}")
    assert reveal.command(f, "darwin") == ["open", "-R", str(f)]
    assert reveal.command(f, "win32") == ["explorer", f"/select,{f}"]
    assert reveal.command(f, "linux") == ["xdg-open", str(tmp_path)]
    assert reveal.command(tmp_path, "linux") == ["xdg-open", str(tmp_path)]


def test_reveal_runs_without_a_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(reveal.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    reveal.reveal(tmp_path)
    assert calls and isinstance(calls[0][0], list) and not calls[0][1].get("shell")


def test_clean_cycle_makes_no_report(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path / "incidents")
    runmarker.write(tmp_path, "http://127.0.0.1:8080")
    runmarker.remove(tmp_path)
    assert runmarker.check_previous(tmp_path, store) is None
    assert store.list() == []


def test_leftover_marker_is_one_redacted_report(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path / "incidents", redactor=Redactor({"T": "sekrit-token-123456"}))
    runmarker.write(tmp_path, "http://u:sekrit-token-123456@127.0.0.1:8080")
    rec = runmarker.check_previous(tmp_path, store)
    assert rec is not None and rec["kind"] == "engine_unclean_exit"
    full = store.get(rec["id"]) or {}
    assert "sekrit-token-123456" not in json.dumps(full)
    assert full["context"]["previous_pid"] and full["context"]["version"]
    assert not runmarker.marker_path(tmp_path).exists()
    assert runmarker.check_previous(tmp_path, store) is None  # once


def test_mac_app_skips_the_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_PARENT_PID", "1")
    runmarker.write(tmp_path, "x")
    assert not runmarker.marker_path(tmp_path).exists()
    runmarker.marker_path(tmp_path).write_text("{}")
    assert runmarker.check_previous(tmp_path, IncidentStore(tmp_path / "i")) is None
