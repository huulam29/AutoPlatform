"""MCP servers: config, ${KEY} filling, the connection test, nodes that allow them."""

from __future__ import annotations

import asyncio
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
from agentline.errors import ValidationError
from agentline.mcp import codex_args, parse_mcp, resolved
from agentline.mcp import test_server as check_server
from agentline.pipeline import parse_spec
from agentline.pipeline.validate import validate_spec
from agentline.registry import Limits, RunRequest
from agentline.runners import ClaudeCodeRunner
from agentline.runners.codex import build_argv as codex_argv
from tests.fakebin import WINDOWS, fake_bin

FAKE_SERVER = Path(__file__).with_name("fake_mcp_server.py")
FAKE_CLAUDE = Path(__file__).parents[1] / "runners" / "fake_claude.py"


def test_parse_and_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    s = parse_mcp("gh", {"command": "npx", "args": ["-y", "srv"], "env": {"T": "${GH_TOKEN}"}})
    assert s.transport == "stdio" and s.key_names() == ["GH_TOKEN"]
    with pytest.raises(ValidationError, match="GH_TOKEN"):
        resolved(s)
    monkeypatch.setenv("GH_TOKEN", "abc")
    assert resolved(s) == {"type": "stdio", "command": "npx", "args": ["-y", "srv"],
                           "env": {"T": "abc"}}  # fmt: skip
    h = parse_mcp("lin", {"url": "https://x/mcp", "headers": {"A": "Bearer ${K}"}})
    assert h.transport == "http" and h.key_names() == ["K"]
    for bad, msg in [
        ({}, "needs `command`"),
        ({"transport": "sse"}, "needs `url`"),
        ({"command": "x", "transport": "ws"}, "transport must be"),
        ({"command": "x", "env": {"T": "ghp_abcdefghijklmnop"}}, "looks like a secret"),
        ({"command": "x", "args": "nope"}, "list of strings"),
    ]:
        with pytest.raises(ValidationError, match=msg):
            parse_mcp("x", bad)


def test_connection_test_lists_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_T", "tok-123")
    s = parse_mcp(
        "fake",
        {
            "command": sys.executable,
            "args": [str(FAKE_SERVER)],
            "env": {"FAKE_TOKEN": "${SECRET_T}"},
        },
    )
    r = check_server(s, timeout=10)
    assert r["ok"] and r["server"]["name"] == "fake"
    assert r["tools"] == [{"name": "search", "description": "token ok: True"}]
    gone = parse_mcp("gone", {"command": "definitely-not-a-binary-xyz"})
    assert "isn't installed" in check_server(gone)["error"]


@pytest.fixture
def client(tmp_path: Path) -> tuple[TestClient, Path]:
    cfg_path = tmp_path / "agentline.toml"
    cfg_path.write_text("")
    (tmp_path / "pipelines").mkdir()
    app = App(SqliteLedger(tmp_path / "l.db"))
    server = create_server(
        app, [], pipelines_dir=tmp_path / "pipelines", config=Config.load(cfg_path)
    )
    return TestClient(server), cfg_path


def test_settings_api_crud_and_test(client: tuple[TestClient, Path]) -> None:
    c, path = client
    body = {
        "command": sys.executable,
        "args": [str(FAKE_SERVER)],
        "env": {"FAKE_TOKEN": "${MCP_T}"},
    }
    r = c.put("/api/settings/mcp/fake", json=body)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["mcp"][0]["name"] == "fake" and s["mcp"][0]["keys"] == ["MCP_T"]
    assert "MCP_T" in {k["name"] for k in s["keys"]}  # shows up under Keys
    assert tomllib.loads(path.read_text())["mcp"]["fake"]["env"] == {"FAKE_TOKEN": "${MCP_T}"}
    r = c.post("/api/mcp/fake/test").json()
    assert not r["ok"] and "MCP_T" in r["error"]  # key not set yet
    bad = c.put("/api/settings/mcp/x", json={"command": "x", "env": {"A": "sk-abcdefghijklmn"}})
    assert bad.status_code == 422
    assert "fake" in c.get("/api/node-types").json()["mcp"]
    assert c.delete("/api/settings/mcp/fake").status_code == 200
    assert c.post("/api/mcp/fake/test").status_code == 404


SPEC = """\
name: p
nodes:
  a: {{ type: agent, prompt: hi, agent: {agent}, mcp: [{servers}] }}
"""


def test_validation_names_and_kinds() -> None:
    cfg = Config.from_dict({"mcp": {"gh": {"command": "npx"}}})
    ok = validate_spec(parse_spec(SPEC.format(agent="claude-code", servers="gh")), None, cfg)
    assert ok == []
    msgs = [
        i.message
        for i in validate_spec(
            parse_spec(SPEC.format(agent="claude-code", servers="gh, ghost")), None, cfg
        )
    ]
    assert any("unknown MCP server(s) ghost" in m for m in msgs)
    msgs = [
        i.message
        for i in validate_spec(parse_spec(SPEC.format(agent="llm", servers="gh")), None, cfg)
    ]
    assert any("can't use MCP servers" in m for m in msgs)


def test_claude_code_gets_only_the_nodes_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = fake_bin(tmp_path, "claude", script=FAKE_CLAUDE)
    log = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    servers = {"gh": {"type": "stdio", "command": "npx", "args": [], "env": {"T": "secret-1"}}}
    req = RunRequest(
        "do it",
        {},
        Limits(allowed_tools=("read",), max_turns=3),
        params={"bin": str(wrapper), "env": ["FAKE_CLAUDE_LOG"], "mcp_servers": servers},
    )
    asyncio.run(ClaudeCodeRunner().run(req))
    seen: dict[str, Any] = json.loads(log.read_text())
    argv = seen["argv"]
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob,mcp__gh"
    mode = "0o666" if WINDOWS else "0o600"  # Windows has no owner-only mode bits
    assert seen["mcp_config"] == {"data": {"mcpServers": servers}, "mode": mode}
    assert "secret-1" not in " ".join(argv)  # the secret is in the file, not argv
    assert not Path(argv[argv.index("--mcp-config") + 1]).exists()  # removed after the run


def test_codex_overrides() -> None:
    servers = {
        "gh": {"type": "stdio", "command": "npx", "args": ["-y", "s"], "env": {"T": "v"}},
        "lin": {"type": "http", "url": "https://x/mcp"},
    }
    assert codex_args(servers) == [
        "-c", 'mcp_servers.gh.command="npx"',
        "-c", 'mcp_servers.gh.args=["-y", "s"]',
        "-c", 'mcp_servers.gh.env={ "T" = "v" }',
        "-c", 'mcp_servers.lin.url="https://x/mcp"',
    ]  # fmt: skip
    req = RunRequest("hi", {}, Limits(), params={"mcp_servers": servers})
    argv = codex_argv(req)
    assert argv[-2:] == ["--", "hi"] and 'mcp_servers.lin.url="https://x/mcp"' in argv
