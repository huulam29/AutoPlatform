"""Pipeline history: saves are commits in a private repo; any version can be restored."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger, history
from agentline.api.server import create_server

V1 = (
    "version: 1\nname: h\nnodes:\n  a: { type: action, uses: shell.run, with: { cmd: echo one } }\n"
)
V2 = V1.replace("echo one", "echo two")


@pytest.fixture
def c(tmp_path: Path) -> tuple[TestClient, Path]:
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "h.yaml").write_text(V1)
    app = App(SqliteLedger(tmp_path / "l.db"))
    return TestClient(create_server(app, [], pipelines_dir=pipelines)), pipelines


@pytest.mark.skipif(not history.available(), reason="needs git")
def test_saves_are_commits_and_restore_works(c: tuple[TestClient, Path]) -> None:
    client, pipelines = c
    assert client.put("/api/pipelines/h/source", json={"source": V2}).status_code == 200
    commits = client.get("/api/history?name=h").json()["commits"]
    assert [x["message"] for x in commits] == ["edit h (YAML)", "before edit"]
    old = commits[1]["commit"]
    assert client.get(f"/api/history/show?name=h&commit={old}").json()["source"] == V1
    r = client.post("/api/history/restore", json={"name": "h", "commit": old}).json()
    assert r["source"] == V1 and (pipelines / "h.yaml").read_text() == V1
    assert client.get("/api/history?name=h").json()["commits"][0]["message"].startswith("restore h")
    assert client.get("/api/history/show?name=h&commit=zzz").status_code == 404
    # private repo: the folder itself isn't a git repository, and the editor ignores it
    assert not (pipelines / ".git").exists() and (pipelines / ".agentline-history").is_dir()
    out = subprocess.run(["git", "-C", str(pipelines), "status"], capture_output=True, text=True)
    assert out.returncode != 0 or "agentline-history" not in out.stdout
    assert [p["name"] for p in client.get("/api/pipelines").json()["pipelines"]] == ["h"]


def test_record_without_changes_is_a_noop(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("x")
    first = history.record(tmp_path, "one")
    assert history.record(tmp_path, "two") is None
    assert first is None or len(first) == 40
