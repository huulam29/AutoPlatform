"""MCP servers found in other tools' configs, import (secrets split out), enable/disable,
used-by, and the test of a server that isn't saved yet."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api.server import create_server
from agentline.config import Config
from agentline.mcp import parse_mcp
from agentline.mcp_detect import detect, public
from agentline.pipeline import parse_spec
from agentline.pipeline.compile import _mcp_servers
from agentline.secrets import read_secrets

FAKE_SERVER = Path(__file__).with_name("fake_mcp_server.py")
GH_TOKEN = "ghp_abcdefghijklmnopqrstuvwx"
LIN_TOKEN = "lin_api_9f8e7d6c5b4a39281706"


def fake_home(home: Path, project: Path) -> None:
    (home / ".claude.json").parent.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "type": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github"],
                        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": GH_TOKEN, "LOG_LEVEL": "info"},
                    }
                },
                "projects": {
                    "/work/app": {
                        "mcpServers": {
                            "linear": {
                                "type": "http",
                                "url": "https://mcp.linear.app/mcp",
                                "headers": {"Authorization": f"Bearer {LIN_TOKEN}"},
                            }
                        }
                    }
                },
            }
        )
    )
    desktop = home / "Library" / "Application Support" / "Claude"
    desktop.mkdir(parents=True)
    (desktop / "claude_desktop_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "files": {
                        "command": "npx",
                        "args": ["-y", "server-fs", "--api-key", "sk-desktopsecret1234567"],
                    },
                    "broken": "not a table",
                }
            }
        )
    )
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.sentry]\nurl = "https://mcp.sentry.dev/mcp"\n'
        'bearer_token_env_var = "SENTRY_TOKEN"\n\n'
        '[mcp_servers.github]\ncommand = "npx"\n'
        'args = ["-y", "@modelcontextprotocol/server-github"]\n'
        'env = { GITHUB_PERSONAL_ACCESS_TOKEN = "ghp_another0000000000000000" }\n'
    )
    project.mkdir(parents=True, exist_ok=True)
    (project / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"db": {"command": "db-mcp", "env": {"DB_URL": "${DATABASE_URL:-x}"}}}}
        )
    )


def test_detect_splits_secrets_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home, project = tmp_path / "home", tmp_path / "proj"
    fake_home(home, project)
    found = {f.id: f for f in detect(home, [project], current_keys={})}
    gh = found["claude-code:github"]
    assert gh.table["env"] == {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}",
        "LOG_LEVEL": "info",  # not a secret: stays as it is
    }
    assert gh.secrets == {"GITHUB_PERSONAL_ACCESS_TOKEN": GH_TOKEN}
    lin = found["claude-code:/work/app:linear"]
    assert lin.table["headers"] == {"Authorization": "Bearer ${LINEAR_TOKEN}"}
    assert lin.secrets == {"LINEAR_TOKEN": LIN_TOKEN}
    files = found["claude-desktop:files"]
    assert files.table["args"] == ["-y", "server-fs", "--api-key", "${FILES_API_KEY}"]
    assert found["claude-desktop:broken"].problem
    sentry = found["codex:sentry"]
    assert sentry.table["headers"] == {"Authorization": "Bearer ${SENTRY_TOKEN}"}
    assert sentry.secrets == {}  # already an env var reference: nothing to copy
    # the same key name with another value gets a server-specific name, never overwritten
    other = found["codex:github"]
    assert other.table["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == (
        "${GITHUB_PERSONAL_ACCESS_TOKEN_GITHUB}"
    )
    db = next(f for f in found.values() if f.name == "db")
    assert db.table["env"] == {"DB_URL": "${DATABASE_URL}"} and not db.secrets
    for f in found.values():  # what goes to agentline.toml parses and holds no secret
        if not f.problem:
            parse_mcp(f.name, f.table)
            text = json.dumps(f.table)
            assert GH_TOKEN not in text and LIN_TOKEN not in text and "sk-desk" not in text
    shown = json.dumps(public(list(found.values()), {}))
    assert GH_TOKEN not in shown and LIN_TOKEN not in shown and "sk-desktop" not in shown
    names = [p["suggested"] for p in public(list(found.values()), {}) if p["status"] == "new"]
    assert len(names) == len(set(names))  # two "github"s get different names


def test_reuses_a_key_set_to_the_same_value(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_home(home, tmp_path / "proj")
    found = detect(home, [], current_keys={"GITHUB_PERSONAL_ACCESS_TOKEN": GH_TOKEN})
    gh = next(f for f in found if f.id == "claude-code:github")
    assert gh.table["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "${GITHUB_PERSONAL_ACCESS_TOKEN}"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path, Path]:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINEAR_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    pipelines = tmp_path / "pipelines"
    fake_home(home, pipelines)
    cfg_path = tmp_path / "agentline.toml"
    cfg_path.write_text("")
    app = App(SqliteLedger(tmp_path / "l.db"))
    server = create_server(app, [], pipelines_dir=pipelines, config=Config.load(cfg_path))
    return TestClient(server), cfg_path, pipelines


def test_api_detect_and_import(client: tuple[TestClient, Path, Path]) -> None:
    c, cfg_path, _ = client
    r = c.get("/api/mcp/detect")
    assert r.status_code == 200, r.text
    found = {f["id"]: f for f in r.json()["found"]}
    assert {"claude-code:github", "codex:sentry", "claude-desktop:files"} <= set(found)
    assert any(f["source"].startswith("Project") for f in found.values())  # pipelines/.mcp.json
    assert GH_TOKEN not in r.text and LIN_TOKEN not in r.text
    assert found["claude-code:github"]["secrets"] == [
        {"key": "GITHUB_PERSONAL_ACCESS_TOKEN", "from": "env GITHUB_PERSONAL_ACCESS_TOKEN"}
    ]
    r = c.post(
        "/api/mcp/import",
        json={"items": [{"id": "claude-code:github"}, {"id": "claude-code:/work/app:linear"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == ["github", "linear"]
    assert GH_TOKEN not in r.text
    toml = cfg_path.read_text()
    assert GH_TOKEN not in toml and LIN_TOKEN not in toml
    raw = tomllib.loads(toml)["mcp"]
    assert raw["linear"]["headers"] == {"Authorization": "Bearer ${LINEAR_TOKEN}"}
    assert read_secrets() == {"GITHUB_PERSONAL_ACCESS_TOKEN": GH_TOKEN, "LINEAR_TOKEN": LIN_TOKEN}
    again = {f["id"]: f for f in c.get("/api/mcp/detect").json()["found"]}
    assert again["claude-code:github"]["status"] == "configured"

    def imp(item: dict[str, str]) -> Any:
        return c.post("/api/mcp/import", json={"items": [item]})

    dup = imp({"id": "claude-code:github", "name": "github"})
    assert dup.status_code == 422 and "already exists" in dup.text
    assert imp({"id": "nope"}).status_code == 422
    assert imp({"id": "claude-desktop:broken"}).status_code == 422


def test_enable_used_by_and_draft_test(client: tuple[TestClient, Path, Path]) -> None:
    c, cfg_path, pipelines = client
    body = {"command": sys.executable, "args": [str(FAKE_SERVER)], "env": {"FAKE_TOKEN": "${T1}"}}
    # a server that isn't saved: the typed key value is used for the test only
    r = c.post("/api/mcp/test", json={"name": "fake", "body": body, "secrets": {"T1": "tok-123"}})
    assert r.json()["ok"] and r.json()["tools"][0]["description"] == "token ok: True"
    assert "T1" not in read_secrets()
    bad = c.post("/api/mcp/test", json={"body": {"transport": "stdio"}}).json()
    assert not bad["ok"] and "command" in bad["error"]
    (pipelines / "p.yaml").write_text(
        "version: 1\nname: p\nnodes:\n  a: {type: agent, prompt: hi, mcp: [fake]}\n"
    )
    assert c.put("/api/settings/mcp/fake", json={**body, "enabled": False}).status_code == 200
    s = c.get("/api/settings").json()["mcp"][0]
    assert s["enabled"] is False and s["used_by"] == [{"pipeline": "p", "node": "a"}]
    assert tomllib.loads(cfg_path.read_text())["mcp"]["fake"]["enabled"] is False
    # a node that lists a disabled server runs without it
    cfg = Config.load(cfg_path)
    node = parse_spec((pipelines / "p.yaml").read_text()).nodes["a"]
    assert _mcp_servers(node, cfg) == {}
    with pytest.raises(Exception, match="enabled"):
        parse_mcp("x", {"command": "x", "enabled": "no"})
