"""What the Mac app's bridge answers, for a browser (api/app.py, incidents export; spec 006)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import tomllib
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger, reveal
from agentline.api import create_server
from agentline.api.app import is_local_request
from agentline.incidents import IncidentStore
from agentline.live import LivePipelines
from agentline.redact import Redactor

from ..pipeline.fakes import World, make_registry
from ..pipeline.test_spec import EXAMPLE

FWD = {"X-Forwarded-For": "203.0.113.9"}


@pytest.fixture
def app(tmp_path: Path) -> Iterator[App]:
    ledger = SqliteLedger(tmp_path / "ledger.db")
    store = IncidentStore(redactor=Redactor({"SOME_KEY": "very-secret-value-42"}))
    app = App(ledger, backoff_base=0.0, artifact_root=tmp_path / "artifacts", incidents=store)
    yield app
    ledger.close()


@pytest.fixture
def pdir(tmp_path: Path) -> Path:
    d = tmp_path / "pipelines"
    shutil.copytree(EXAMPLE.parent, d / "ticket-to-eod")
    return d


@pytest.fixture(autouse=True)
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_CONFIG", str(tmp_path / "agentline.toml"))


def make(app: App, pdir: Path, tmp_path: Path, token: str = "", live: bool = False) -> TestClient:
    pipelines: Any = []
    if live:
        pipelines = LivePipelines(
            app,
            pdir,
            tmp_path / "agentline.toml",
            registry_factory=lambda c: make_registry(World()),
        )
    # TestClient's client host is "testclient": count it as loopback for these tests
    return TestClient(
        create_server(app, pipelines, pipelines_dir=pdir, token=token), client=("127.0.0.1", 5000)
    )


@pytest.fixture
def client(app: App, pdir: Path, tmp_path: Path) -> Iterator[TestClient]:
    with make(app, pdir, tmp_path) as c:
        yield c


class Req:
    def __init__(self, host: str, headers: dict[str, str] | None = None) -> None:
        self.client = type("C", (), {"host": host})()
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


def test_local_means_loopback_without_proxy_headers() -> None:
    assert is_local_request(Req("127.0.0.1"))  # type: ignore[arg-type]
    assert is_local_request(Req("::1"))  # type: ignore[arg-type]
    assert not is_local_request(Req("192.168.1.5"))  # type: ignore[arg-type]
    assert not is_local_request(Req("127.0.0.1", FWD))  # type: ignore[arg-type]
    assert not is_local_request(Req("127.0.0.1", {"Forwarded": "for=1.2.3.4"}))  # type: ignore[arg-type]


def test_app_info(client: TestClient, pdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    info = client.get("/api/app/info").json()
    assert info["pipelines_dir"] == str(pdir.resolve())
    assert info["data_folder"] == os.environ["AGENTLINE_HOME"]
    assert info["config_path"].endswith("agentline.toml")
    assert info["mac_app"] is False and info["local"] is True and info["token_required"] is False
    assert info["can_reveal"] is True
    assert info["can_restart"] is False  # no serve loop behind this server
    assert client.get("/api/app/info", headers=FWD).json()["local"] is False
    monkeypatch.setenv("AGENTLINE_PARENT_PID", "1")
    assert client.get("/api/app/info").json()["mac_app"] is True


def test_auth_check_never_returns_the_token(app: App, pdir: Path, tmp_path: Path) -> None:
    with make(app, pdir, tmp_path) as open_client:
        assert open_client.get("/api/auth/check").json() == {"token_required": False, "ok": True}
    with make(app, pdir, tmp_path, token="s3cret-token") as c:
        assert c.get("/api/app/info").status_code == 401
        no = c.get("/api/auth/check")
        bad = c.get("/api/auth/check", headers={"Authorization": "Bearer nope"})
        good = c.get("/api/auth/check", headers={"Authorization": "Bearer s3cret-token"})
        assert no.json() == bad.json() == {"token_required": True, "ok": False}
        assert good.json() == {"token_required": True, "ok": True}
        assert all("s3cret-token" not in r.text for r in (no, bad, good))


def test_restart_sets_the_flag_and_stop_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = client.app.state  # type: ignore[attr-defined]
    assert client.post("/api/engine/restart").status_code == 409  # no serve loop: refused
    state.stop = asyncio.Event()
    state.restart = False
    r = client.post("/api/engine/restart")
    assert r.status_code == 202 and r.json() == {"restarting": True}
    assert state.restart is True and state.stop.is_set()
    again = client.post("/api/engine/restart")
    assert again.status_code == 409 and "already" in again.json()["detail"]
    state.restart = False
    assert client.post("/api/engine/restart", headers=FWD).status_code == 403  # remote, no token
    monkeypatch.setenv("AGENTLINE_PARENT_PID", "1")
    assert "Mac app" in client.post("/api/engine/restart").json()["detail"]


def test_remote_restart_needs_the_token(app: App, pdir: Path, tmp_path: Path) -> None:
    with make(app, pdir, tmp_path, token="tok-abcdef") as c:
        c.app.state.stop = asyncio.Event()  # type: ignore[attr-defined]
        c.app.state.restart = False  # type: ignore[attr-defined]
        auth = {"Authorization": "Bearer tok-abcdef", **FWD}
        assert c.get("/api/app/info", headers=auth).json()["can_restart"] is True
        assert c.post("/api/engine/restart", headers=auth).status_code == 202


def test_folders_lists_only_visible_folders(client: TestClient, tmp_path: Path) -> None:
    base = tmp_path / "browse"
    (base / "b").mkdir(parents=True)
    (base / "a").mkdir()
    (base / ".hidden").mkdir()
    (base / "file.txt").write_text("x")
    r = client.get("/api/app/folders", params={"path": str(base)}).json()
    assert [f["name"] for f in r["folders"]] == ["a", "b"]
    assert r["parent"] == str(tmp_path.resolve()) and r["path"] == str(base.resolve())
    assert (
        client.get("/api/app/folders", params={"path": str(base / "file.txt")}).status_code == 400
    )
    assert client.get("/api/app/folders").json()["path"] == str(Path.home().resolve())


def test_set_pipelines_folder_is_saved_and_live(app: App, pdir: Path, tmp_path: Path) -> None:
    toml = tmp_path / "agentline.toml"
    toml.write_text('# mine\n\n[repos.app]\nremote = "/x.git"\n')
    other = tmp_path / "other"
    shutil.copytree(EXAMPLE.parent, other / "renamed")
    with make(app, pdir, tmp_path, live=True) as c:
        assert [p["name"] for p in c.get("/api/pipelines").json()["pipelines"]] == [
            "ticket-to-eod/pipeline"
        ]
        r = c.put("/api/app/pipelines-dir", json={"path": str(other)})
        assert r.status_code == 200, r.text
        assert r.json() == {"pipelines_dir": str(other.resolve()), "seeded": False, "note": None}
        assert [p["name"] for p in c.get("/api/pipelines").json()["pipelines"]] == [
            "renamed/pipeline"
        ]
        assert c.get("/api/app/info").json()["pipelines_dir"] == str(other.resolve())
    text = toml.read_text()
    assert text.startswith("# mine\n") and "[repos.app]" in text
    assert tomllib.loads(text)["pipelines_dir"] == str(other.resolve())


def test_set_pipelines_folder_refuses_bad_paths_and_seeds_empty(
    client: TestClient, tmp_path: Path
) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x")
    for bad in (str(f), str(tmp_path / "missing"), "relative/dir", ""):
        assert client.put("/api/app/pipelines-dir", json={"path": bad}).status_code == 400
    empty = tmp_path / "empty"
    empty.mkdir()
    r = client.put("/api/app/pipelines-dir", json={"path": str(empty)}).json()
    assert r["seeded"] is True and (empty / "ticket-to-eod" / "pipeline.yaml").exists()


def test_pipelines_from_the_command_line_get_a_note(client: TestClient, tmp_path: Path) -> None:
    client.app.state.pipelines_from_cli = True  # type: ignore[attr-defined]
    d = tmp_path / "d"
    shutil.copytree(EXAMPLE.parent, d / "p")
    assert (
        "--pipelines" in client.put("/api/app/pipelines-dir", json={"path": str(d)}).json()["note"]
    )


def test_reveal_is_local_only_and_inside_the_allowed_folders(
    client: TestClient, pdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(reveal, "reveal", lambda p: opened.append(str(p)))
    Path(os.environ["AGENTLINE_HOME"]).mkdir(parents=True, exist_ok=True)
    assert client.post("/api/app/reveal", json={"target": "data"}).status_code == 200
    assert client.post("/api/app/reveal", json={"target": "pipelines"}).json()["revealed"] == str(
        pdir.resolve()
    )
    assert (
        client.post("/api/app/reveal", json={"path": str(pdir / "ticket-to-eod")}).status_code
        == 200
    )
    outside = tmp_path.parent
    assert client.post("/api/app/reveal", json={"path": str(outside)}).status_code == 400
    assert client.post("/api/app/reveal", json={"target": "nope"}).status_code == 400
    assert client.post("/api/app/reveal", json={"target": "data"}, headers=FWD).status_code == 403
    assert len(opened) == 3


def test_export_zip_has_the_mac_layout_and_is_redacted(client: TestClient, app: App) -> None:
    import logging

    store = app.incidents
    assert store is not None
    rec = store.record("api_error", title="boom", message="it broke")
    (store.root / rec["id"]).mkdir()
    (store.root / rec["id"] / "note.txt").write_text("attached")
    data = json.loads((store.root / f"{rec['id']}.json").read_text())
    data["attachments"] = [f"{rec['id']}/note.txt", "../escape.txt"]
    (store.root / f"{rec['id']}.json").write_text(json.dumps(data))
    from agentline.logs import RING

    RING.handle(
        logging.LogRecord("x", logging.INFO, "f", 1, "key very-secret-value-42 used", None, None)
    )
    r = client.get("/api/incidents/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"].startswith('attachment; filename="agentline-report-')
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {f"incidents/{rec['id']}.json", f"incidents/{rec['id']}/note.txt"} <= names
    assert {"system.txt", "engine.log", "summary.md"} <= names
    assert not any("escape" in n for n in names)
    assert "very-secret-value-42" not in z.read("engine.log").decode()
    assert "boom" in z.read("summary.md").decode()
