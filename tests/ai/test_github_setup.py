"""GitHub setup for triage reports: remotes, [report] writes, token sources, CLI sign-in, test."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline import github_setup as gs
from agentline.api.server import create_server
from agentline.config import Config
from agentline.errors import ValidationError
from agentline.tomlwrite import set_plain_table

FAKE_GH = """#!/bin/sh
# a stand-in GitHub CLI: login prints a code, waits for the "browser", then succeeds
state="$FAKE_GH_STATE"
case "$1 $2" in
  "auth token") [ -f "$state" ] && echo gho_fake_cli_token && exit 0; exit 1 ;;
  "auth status") [ -f "$state" ] || exit 1
    echo "Logged in to github.com account octo (keyring)"; exit 0 ;;
  "auth login")
    read _
    echo "! One-time code (AB12-CD34) copied to clipboard"
    echo "Open this URL to continue in your web browser: https://github.com/login/device"
    while [ ! -f "$state.approve" ]; do sleep 0.05; done
    touch "$state"; echo "Logged in as octo"; exit 0 ;;
esac
exit 2
"""


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "gh-state"
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    monkeypatch.setattr(gs, "gh_path", lambda: str(gh))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    gs._cache.clear()
    gs.login.__init__()  # type: ignore[misc]
    return state


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:  # skip socket.getfqdn() (slow on macOS)
        import socketserver

        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]  # type: ignore[misc]


@pytest.fixture
def github() -> Iterator[tuple[str, list[str]]]:
    seen: list[str] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(f"{self.path} {self.headers['Authorization']}")
            ok = self.path == "/repos/o/r"
            data = json.dumps(
                {"full_name": "o/r", "private": True, "has_issues": True}
                if ok
                else {"message": "Not Found"}
            ).encode()
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = _Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    finally:
        srv.shutdown()


def test_remotes_ssh_and_https_become_owner_name(tmp_path: Path) -> None:
    for url in (
        "git@github.com:o/r.git",
        "ssh://git@github.com/o/r",
        "ssh://git@github.com:22/o/r.git",
    ):
        assert gs.parse_remote(url) == ("o/r", "ssh"), url
    for url in ("https://github.com/o/r.git", "https://x-token@github.com/o/r/"):
        assert gs.parse_remote(url) == ("o/r", "https"), url
    assert gs.parse_remote("git@gitlab.com:o/r.git") is None
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:me/agentline.git"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "up", "https://github.com/me/agentline"],
        check=True,
    )
    found = gs.detect_repos([("agentline", repo), ("nowhere", tmp_path / "missing")])
    assert found == [{"repo": "me/agentline", "remote": "origin", "protocol": "ssh",
                      "where": "agentline", "url": "git@github.com:me/agentline.git"}]  # fmt: skip


def test_report_table_writes_keep_everything_else(tmp_path: Path) -> None:
    text = '# mine\n[models.x]\nprovider = "p"\nmodel = "m"\n'
    new = set_plain_table(text, "report", {"github_repo": "o/r"})
    assert new.startswith(text) and new.endswith('[report]\ngithub_repo = "o/r"\n')
    again = set_plain_table(new, "report", {"github_repo": "o/s", "github_auto": True})
    assert again.count("[report]") == 1 and 'github_repo = "o/s"' in again
    assert set_plain_table(again, "report", {}).rstrip() == text.rstrip()
    with pytest.raises(ValidationError):
        set_plain_table('report.github_repo = "x"\n', "report", {"github_repo": "o/r"})
    path = tmp_path / "agentline.toml"
    path.write_text(text)
    gs.save(path, "git@github.com:me/agentline.git", auto=None)  # an SSH remote is fine
    assert 'github_repo = "me/agentline"' in path.read_text()
    gs.save(path, "me/agentline", auto=True)
    assert "github_auto = true" in path.read_text()
    gs.save(path, "me/agentline", auto=False)
    assert "github_auto" not in path.read_text()
    with pytest.raises(ValidationError):
        gs.save(path, "not a repo", None)
    assert Config.load(path).raw["report"] == {"github_repo": "me/agentline"}


def test_a_pasted_token_wins_over_the_cli(fake_gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentline.secrets import set_secret

    monkeypatch.setenv("GITHUB_TOKEN", "")  # set_secret writes os.environ: undo it after
    assert gs.token() == ("", None)
    fake_gh.touch()  # signed in with the CLI
    gs._cache.clear()
    assert gs.token() == ("gho_fake_cli_token", "gh") and gs.gh_user() == "octo"
    set_secret("GITHUB_TOKEN", "ghp_from_file")
    assert gs.token() == ("ghp_from_file", "secrets")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env")
    assert gs.token() == ("ghp_from_env", "env")


def test_browser_sign_in_through_the_cli(fake_gh: Path) -> None:
    v = gs.login.start(wait=5)
    assert v["state"] == "waiting" and v["code"] == "AB12-CD34"
    assert v["url"] == "https://github.com/login/device"
    assert gs.login.start(wait=0)["code"] == "AB12-CD34"  # one at a time
    Path(str(fake_gh) + ".approve").touch()  # the user approves in the browser
    for _ in range(100):
        if gs.login.view()["state"] != "waiting":
            break
        time.sleep(0.05)
    assert gs.login.view()["state"] == "done"
    assert gs.token() == ("gho_fake_cli_token", "gh")


def test_cancel_and_no_cli(fake_gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gs.login.start(wait=5)
    assert gs.login.cancel()["state"] == "cancelled"
    monkeypatch.setattr(gs, "gh_path", lambda: None)
    with pytest.raises(ValidationError, match="isn't installed"):
        gs.login.start()


def test_api_save_status_and_test(
    tmp_path: Path, fake_gh: Path, github: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, seen = github
    monkeypatch.setenv("GITHUB_API_URL", url)
    cfg_path = tmp_path / "agentline.toml"
    cfg_path.write_text("")
    (tmp_path / "pipelines").mkdir()
    app = App(SqliteLedger(tmp_path / "l.db"))
    c = TestClient(create_server(app, [], pipelines_dir=tmp_path / "pipelines",
                                 config=Config.load(cfg_path)))  # fmt: skip
    st = c.get("/api/github").json()
    assert st["repo"] is None and st["ready"] is False and st["gh"]["installed"] is True
    assert "token" not in json.dumps(st).replace("token_source", "")
    assert c.post("/api/github/test").json() == {"ok": False, "error": "choose the repo first"}
    st = c.put("/api/github", json={"repo": "https://github.com/o/r.git"}).json()
    assert st["repo"] == "o/r" and st["token_source"] is None
    assert "sign in" in c.post("/api/github/test").json()["error"]
    assert c.post("/api/github/login").json()["code"] == "AB12-CD34"
    Path(str(fake_gh) + ".approve").touch()
    for _ in range(100):
        if c.get("/api/github/login").json()["state"] == "done":
            break
        time.sleep(0.05)
    st = c.get("/api/github").json()
    assert st["ready"] is True and st["token_source"] == "gh" and st["gh"]["user"] == "octo"
    r = c.post("/api/github/test").json()
    assert r == {"ok": True, "repo": "o/r", "private": True, "token_source": "gh", "error": None}
    assert seen[-1] == "/repos/o/r Bearer gho_fake_cli_token"
    c.put("/api/github", json={"repo": "o/missing"})
    r = c.post("/api/github/test").json()
    assert (
        r["ok"] is False and r["error"] == "o/missing wasn't found, or the token can't see it (404)"
    )
    assert c.put("/api/github", json={"repo": "nope"}).status_code == 422
    assert os.environ.get("GITHUB_TOKEN") is None
