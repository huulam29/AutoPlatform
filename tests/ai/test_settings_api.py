"""Settings API and the stores behind it: agentline.toml tables and the secrets file."""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api.server import create_server
from agentline.api.settings import RESTART_NOTE
from agentline.config import Config
from agentline.errors import ValidationError
from agentline.secrets import delete_secret, load_secrets, read_secrets, secret_status, set_secret
from agentline.tomlwrite import delete_table, set_table
from tests.fakebin import WINDOWS, fake_bin

from .fakes import KEY, fake_llm

ORIGINAL = """\
# agentline config: comments survive edits
allow_images = ["agentline/"]

[repos.app]
remote = "git@example.com:acme/app.git"  # the repo

[providers.local]
type = "openai-compatible"
base_url = "http://127.0.0.1:9/v1"

# deploy targets
[environments.eod]
deploy = "./deploy.sh"
"""


def test_set_and_delete_table_keep_everything_else() -> None:
    added = set_table(ORIGINAL, "models", "cheap", {"provider": "local", "model": "llama3"})
    assert added.startswith(ORIGINAL) and added.endswith(
        '[models.cheap]\nprovider = "local"\nmodel = "llama3"\n'
    )
    changed = set_table(
        added, "providers", "local", {"type": "openai-compatible", "base_url": "http://10.0.0.5/v1"}
    )
    assert "# deploy targets\n[environments.eod]" in changed
    assert '[repos.app]\nremote = "git@example.com:acme/app.git"  # the repo' in changed
    assert tomllib.loads(changed)["providers"]["local"]["base_url"] == "http://10.0.0.5/v1"
    removed = delete_table(changed, "models", "cheap")
    assert "models" not in tomllib.loads(removed)
    assert "# deploy targets" in removed and "# agentline config" in removed
    assert delete_table(removed, "models", "nope") == removed


def test_secrets_file_is_private_and_write_only(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_secret("OPENROUTER_API_KEY", KEY)
    path = isolated_home / "secrets.env"
    if not WINDOWS:  # Windows: the per-user profile's ACL keeps it private, not the mode
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_secrets() == {"OPENROUTER_API_KEY": KEY}
    assert os.environ["OPENROUTER_API_KEY"] == KEY
    status = secret_status(["OPENROUTER_API_KEY", "PRIVATE_LLM_KEY"])
    assert status == [
        {"name": "OPENROUTER_API_KEY", "set": True, "source": "secrets file"},
        {"name": "PRIVATE_LLM_KEY", "set": False, "source": None},
    ]
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-shell")  # the shell wins on load
    load_secrets()
    assert os.environ["OPENROUTER_API_KEY"] == "from-shell"
    with pytest.raises(ValidationError):
        set_secret("not a name", "x")
    with pytest.raises(ValidationError):
        set_secret("X_KEY", "two\nlines")
    assert delete_secret("OPENROUTER_API_KEY") and read_secrets() == {}


@pytest.fixture
def client(tmp_path: Path, isolated_home: Path) -> tuple[TestClient, Path]:
    cfg_path = tmp_path / "agentline.toml"
    cfg_path.write_text(ORIGINAL)
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    app = App(SqliteLedger(tmp_path / "l.db"))
    server = create_server(app, [], pipelines_dir=pipelines, config=Config.load(cfg_path))
    return TestClient(server), cfg_path


def test_settings_crud_writes_agentline_toml(client: tuple[TestClient, Path]) -> None:
    c, path = client
    s = c.get("/api/settings").json()
    assert {p["name"] for p in s["providers"]} == {
        "anthropic", "claude", "openrouter", "openai", "ollama", "local"
    }  # fmt: skip
    assert "claude-code" in {a["name"] for a in s["harnesses"]}
    r = c.put(
        "/api/settings/providers/openrouter",
        json={
            "type": "openai-compatible",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "headers": {},
            "description": "",
        },
    )
    assert r.status_code == 200, r.text
    assert "restarted" in r.json()["note"]
    r = c.put(
        "/api/settings/models/free-llama",
        json={"provider": "openrouter", "model": "meta-llama/llama-3.3-70b-instruct:free"},
    )
    assert r.status_code == 200, r.text
    body = {"kind": "builtin", "model": "free-llama"}
    r = c.put("/api/settings/harnesses/small-tasks", json=body)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {k["name"]: k["set"] for k in body["keys"]}["OPENROUTER_API_KEY"] is False
    # no key yet: the source isn't ready, so pickers don't offer its models
    assert "free-llama" not in body["compatible_models"]["small-tasks"]
    model = next(m for m in body["models"] if m["name"] == "free-llama")
    source = next(p for p in body["providers"] if p["name"] == "openrouter")
    assert not model["ready"] and source["not_ready"] == "OPENROUTER_API_KEY isn't set"
    c.put("/api/secrets/OPENROUTER_API_KEY", json={"value": KEY})
    body = c.get("/api/settings").json()
    assert "free-llama" in body["compatible_models"]["small-tasks"]
    text = path.read_text()
    assert text.startswith(ORIGINAL)  # appended; the rest is untouched
    assert tomllib.loads(text)["harnesses"]["small-tasks"] == {
        "kind": "builtin", "model": "free-llama"
    }  # fmt: skip
    # the editor sees the new names right away
    assert "small-tasks" in c.get("/api/node-types").json()["harnesses"]


@pytest.mark.parametrize(
    ("url", "body", "error"),
    [
        ("/api/settings/providers/x", {"type": "openai-compatible"}, "base_url"),
        (
            "/api/settings/providers/x",
            {"type": "anthropic", "api_key": "sk-ant-1"},
            "keys go in /api/secrets",
        ),
        ("/api/settings/models/m", {"provider": "ghost", "model": "x"}, "unknown provider"),
        ("/api/settings/harnesses/a", {"kind": "gpt-cli"}, "unknown harness kind"),
        ("/api/settings/agents/a", {"kind": "gpt-cli"}, "unknown harness kind"),  # old path
        (
            "/api/settings/harnesses/cc",
            {"kind": "claude-code", "model": "local:llama3"},
            "Anthropic models only",
        ),
        ("/api/settings/providers/bad name", {"type": "anthropic"}, "bad name"),
    ],
)
def test_settings_validation(
    client: tuple[TestClient, Path], url: str, body: dict[str, Any], error: str
) -> None:
    c, path = client
    r = c.put(url, json=body)
    assert r.status_code == 422 and error in r.text, r.text
    assert path.read_text() == ORIGINAL


def test_delete_refuses_while_referenced(client: tuple[TestClient, Path]) -> None:
    c, path = client
    c.put("/api/settings/models/m", json={"provider": "local", "model": "llama3"})
    r = c.delete("/api/settings/providers/local")
    assert r.status_code == 409 and "used by models: m" in r.text
    assert c.delete("/api/settings/models/m").status_code == 200
    assert c.delete("/api/settings/providers/local").status_code == 200
    assert "providers" not in tomllib.loads(path.read_text())


def test_delete_source_with_its_models(client: tuple[TestClient, Path]) -> None:
    c, path = client
    c.put("/api/settings/models/m", json={"provider": "local", "model": "llama3"})
    c.put("/api/settings/models/n", json={"provider": "local", "model": "qwen3"})
    c.put("/api/settings/harnesses/small", json={"kind": "builtin", "model": "n"})
    r = c.delete("/api/settings/providers/local?with_models=true")
    assert r.status_code == 409 and "n (harness small)" in r.text
    assert "providers" in tomllib.loads(path.read_text())
    c.delete("/api/settings/harnesses/small")
    r = c.delete("/api/settings/providers/local?with_models=true")
    assert r.status_code == 200, r.text
    assert r.json()["removed_models"] == ["m", "n"]
    left = tomllib.loads(path.read_text())
    assert "providers" not in left and "models" not in left
    assert "# deploy targets" in path.read_text()


def test_remove_and_restore_a_builtin_source(client: tuple[TestClient, Path]) -> None:
    c, path = client
    c.put("/api/settings/models/mine", json={"provider": "openrouter", "model": "x/y"})
    # a configured harness's default blocks; built-in harnesses just lose their default model
    c.put("/api/settings/harnesses/small", json={"kind": "builtin", "model": "free"})
    r = c.delete("/api/settings/providers/openrouter?with_models=true")
    assert r.status_code == 409 and "free (harness small)" in r.text
    c.delete("/api/settings/harnesses/small")
    r = c.delete("/api/settings/providers/openrouter?with_models=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["removed_models"] == ["mine", "free", "auto"]
    assert "openrouter" not in {p["name"] for p in body["providers"]}
    assert not {"free", "auto", "mine"} & {m["name"] for m in body["models"]}
    assert next(h for h in body["harnesses"] if h["name"] == "builtin")["model"] is None
    assert tomllib.loads(path.read_text())["removed"] == {"providers": ["openrouter"]}
    # a preset adds it back, with its built-in models
    r = c.put(
        "/api/settings/providers/openrouter",
        json={"type": "openai-compatible", "base_url": "https://openrouter.ai/api/v1"},
    )
    assert r.status_code == 200, r.text
    assert {"free", "auto"} <= {m["name"] for m in r.json()["models"]}
    assert "removed" not in tomllib.loads(path.read_text())


def test_subscription_login_status_and_sign_out(
    client: tuple[TestClient, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = client
    bin_dir, state = tmp_path / "bin", tmp_path / "logged-in"
    bin_dir.mkdir()
    state.write_text("")
    code = f"""
import os, sys
state = {str(state)!r}
if sys.argv[2:3] == ["logout"]:
    os.remove(state)
    print("Logged out")
elif os.path.exists(state):
    print('{{"loggedIn": true, "subscriptionType": "max"}}')
else:
    print('{{"loggedIn": false}}')
"""
    fake_bin(bin_dir, "claude", code=code)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), "/bin", "/usr/bin"]))
    r = c.get("/api/providers/claude/login").json()
    assert r["logged_in"] and r["subscription"] == "max"
    assert c.get("/api/providers/local/login").status_code == 422  # not a subscription
    r = c.post("/api/providers/claude/logout")
    assert r.status_code == 200 and not r.json()["logged_in"]
    assert not state.exists()


def test_secrets_api_never_returns_values(
    client: tuple[TestClient, Path], isolated_home: Path
) -> None:
    c, _ = client
    r = c.put("/api/secrets/OPENROUTER_API_KEY", json={"value": KEY})
    assert r.status_code == 200 and KEY not in r.text
    assert KEY not in c.get("/api/settings").text
    assert {k["name"]: k["set"] for k in c.get("/api/settings").json()["keys"]}[
        "OPENROUTER_API_KEY"
    ]
    assert KEY in (isolated_home / "secrets.env").read_text()
    assert c.put("/api/secrets/bad-name", json={"value": "x"}).status_code == 422
    assert c.delete("/api/secrets/OPENROUTER_API_KEY").status_code == 200


def test_provider_test_lists_models_free_first(
    client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = client
    with fake_llm() as (base, _st):
        c.put(
            "/api/settings/providers/openrouter",
            json={
                "type": "openai-compatible",
                "base_url": base,
                "api_key_env": "OPENROUTER_API_KEY",
            },
        )
        missing = c.post("/api/providers/openrouter/test").json()
        assert not missing["ok"] and "OPENROUTER_API_KEY is not set" in missing["error"]
        assert missing["problem"] == "no_key" and missing["key"] == "OPENROUTER_API_KEY"
        monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
        r = c.post("/api/providers/openrouter/test").json()
    assert r["ok"] and r["count"] == 3
    assert [m["free"] for m in r["models"]] == [True, True, False]
    assert r["models"][0]["id"] == "meta-llama/llama-3.3-70b-instruct:free"
    down = c.post("/api/providers/local/test").json()
    assert not down["ok"] and "can't reach" in down["error"]
    assert down["problem"] == "unreachable"


def test_subscription_source_needs_the_claude_cli(
    client: tuple[TestClient, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = client
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    r = c.post("/api/providers/claude/test").json()
    assert not r["ok"] and r["problem"] == "no_cli" and r["bin"] == "claude"
    assert "claude-sonnet-5-5" in [m["id"] for m in r["models"]]  # still listed for pickers
    assert c.post("/api/harnesses/claude-code/detect").json()["problem"] == "no_cli"
    fake_bin(tmp_path / "bin", "claude")
    r = c.post("/api/providers/claude/test").json()  # installed, but not logged in
    assert not r["ok"] and r["problem"] == "signed_out"
    fake_bin(tmp_path / "bin", "claude", code="print('{\"loggedIn\": true}')")
    r = c.post("/api/providers/claude/test").json()
    assert r["ok"] and "opus" in [m["id"] for m in r["models"]] and Path(r["cli"]).stem == "claude"


def test_choose_models_adds_checked_and_removes_unchecked(
    client: tuple[TestClient, Path],
) -> None:
    c, path = client
    r = c.put(
        "/api/providers/local/models",
        json={"models": ["meta-llama/llama-3.3-70b:free", "qwen/qwen3", "x/qwen3"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == ["llama-3.3-70b-free", "qwen3", "qwen3-2"]
    models = tomllib.loads(path.read_text())["models"]
    assert models["qwen3-2"] == {"provider": "local", "model": "x/qwen3"}
    c.put("/api/settings/harnesses/small", json={"kind": "builtin", "model": "qwen3"})
    r = c.put("/api/providers/local/models", json={"models": ["x/qwen3"]}).json()
    assert r["removed"] == ["llama-3.3-70b-free"] and r["kept"] == ["qwen3"]
    assert set(tomllib.loads(path.read_text())["models"]) == {"qwen3", "qwen3-2"}
    # built-in models on a source are never deleted
    r = c.put("/api/providers/claude/models", json={"models": []}).json()
    assert r["removed"] == [] and "opus" in {m["name"] for m in r["models"]}
    assert c.put("/api/providers/ghost/models", json={"models": []}).status_code == 404


def test_harness_detect(client: tuple[TestClient, Path]) -> None:
    c, _ = client
    c.put(
        "/api/settings/harnesses/cc",
        json={"kind": "claude-code", "bin": "definitely-not-installed-xyz"},
    )
    assert c.post("/api/harnesses/cc/detect").json() == {
        "found": False,
        "bin": "definitely-not-installed-xyz",
        "error": "'definitely-not-installed-xyz' is not on PATH",
        "problem": "no_cli",
    }
    c.put("/api/settings/harnesses/sh", json={"kind": "claude-code", "bin": "true"})
    assert c.post("/api/harnesses/sh/detect").json()["found"] is True
    assert c.post("/api/harnesses/builtin/detect").json()["note"].startswith("no CLI needed")


def test_settings_writes_flag_a_restart(client: tuple[TestClient, Path]) -> None:
    from agentline import restart

    c, _ = client
    restart.clear()
    assert c.get("/api/status").json()["restart_needed"] == []
    c.put("/api/settings/models/m", json={"provider": "local", "model": "llama3"})
    assert c.get("/api/status").json()["restart_needed"] == ["models.m changed"]
    restart.clear()


def test_add_only_adds_the_checked_models_and_removes_nothing(
    client: tuple[TestClient, Path],
) -> None:
    c, path = client
    c.put("/api/providers/local/models", json={"models": ["qwen/qwen3"]})
    r = c.put(
        "/api/providers/local/models",
        json={"models": ["meta-llama/llama-3.3-70b:free", "x/qwen3"], "add_only": True},
    ).json()
    assert r["added"] == ["llama-3.3-70b-free", "qwen3-2"] and r["removed"] == []
    assert r["note"] == RESTART_NOTE  # no live engine in this test: the reminder is still true
    models = tomllib.loads(path.read_text())["models"]
    assert {"qwen3", "llama-3.3-70b-free", "qwen3-2"} <= set(models)
    again = c.put(
        "/api/providers/local/models", json={"models": ["x/qwen3"], "add_only": True}
    ).json()
    assert again["added"] == [] and again["removed"] == []
