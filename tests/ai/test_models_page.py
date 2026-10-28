"""Phase 46: the Models page shows only what is usable: connected sources, models hidden from
pickers with the eye, and sources that stopped working ("Needs attention")."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger, readiness
from agentline.api.server import create_server
from agentline.config import Config

from .fakes import FakeLLM, fake_llm

TOML = """\
# mine
[providers.local]
type = "openai-compatible"
base_url = "http://127.0.0.1:9/v1"

[models.mine]
provider = "local"
model = "a/b"
"""


@pytest.fixture
def client(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Path]:
    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "agentline.toml"
    path.write_text(TOML)
    (tmp_path / "p").mkdir()
    app = App(SqliteLedger(tmp_path / "l.db"))
    server = create_server(app, [], pipelines_dir=tmp_path / "p", config=Config.load(path))
    return TestClient(server), path


def compatible(c: TestClient, harness: str = "builtin") -> list[str]:
    return c.get("/api/settings").json()["compatible_models"][harness]


def test_hiding_a_built_in_model_writes_an_override_and_showing_removes_it(client) -> None:
    c, path = client
    assert "free" in compatible(c)
    r = c.put("/api/settings/models/free/visibility", json={"hidden": True})
    assert r.status_code == 200, r.text
    assert r.json()["note"] is None or "restart" in r.json()["note"].lower()
    table = tomllib.loads(path.read_text())["models"]["free"]
    assert table["hidden"] is True and table["provider"] == "openrouter"
    assert "free" not in compatible(c)
    free = next(m for m in c.get("/api/settings").json()["models"] if m["name"] == "free")
    assert free["hidden"] is True
    c.put("/api/settings/models/free/visibility", json={"hidden": False})
    assert "free" not in tomllib.loads(path.read_text())["models"]  # back to the built-in
    assert "free" in compatible(c) and path.read_text().startswith("# mine\n")


def test_hiding_your_own_model_keeps_it_set_up(client) -> None:
    c, path = client
    c.put("/api/settings/models/mine/visibility", json={"hidden": True})
    assert tomllib.loads(path.read_text())["models"]["mine"] == {
        "provider": "local", "model": "a/b", "hidden": True
    }  # fmt: skip
    assert "mine" not in compatible(c)
    c.put("/api/settings/models/mine/visibility", json={"hidden": False})
    assert tomllib.loads(path.read_text())["models"]["mine"] == {
        "provider": "local",
        "model": "a/b",
    }
    assert c.put("/api/settings/models/ghost/visibility", json={"hidden": True}).status_code == 404


def test_connected_and_set_up_by_you(client) -> None:
    c, _ = client
    provs = {p["name"]: p for p in c.get("/api/settings").json()["providers"]}
    assert provs["openrouter"]["connected"] and not provs["openrouter"]["user_set"]
    assert not provs["openai"]["connected"] and not provs["openai"]["user_set"]  # not shown
    assert provs["local"]["connected"] and provs["local"]["user_set"]


def test_a_built_in_local_server_counts_only_while_it_answers() -> None:
    readiness._server_cache.clear()
    with fake_llm(FakeLLM(require_key=None)) as (base, _):
        assert readiness.server_up(base)
    readiness._server_cache.clear()
    assert not readiness.server_up("http://127.0.0.1:9/v1")
    assert not readiness.server_up(None)
