"""Phase 44: only `builtin` is a built-in harness and the fallback; CLI harnesses are added
from presets; a one-time migration keeps what an install already used."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from agentline import agents as ai
from agentline.config import Config
from agentline.errors import ValidationError
from agentline.harness_migration import marker_path, migrate

pytestmark = pytest.mark.builtin_only


def test_only_builtin_is_built_in_and_it_is_the_fallback() -> None:
    assert list(ai.harnesses(None)) == ["builtin"]
    assert ai.resolve(None, "free", None).harness.name == "builtin"
    assert ai.resolve(None, None, None).harness.name == "builtin"
    with pytest.raises(ValidationError, match="isn't set up: add it under Settings > Harnesses"):
        ai.resolve("claude-code", None, None)
    cfg = Config.from_dict({"harnesses": {"claude-code": {"kind": "claude-code"}}})
    assert ai.resolve("claude-code", None, cfg).runner == "claude-code"
    # agentline's own uses (the vision transcriber) may run a preset that isn't set up
    assert ai.resolve("claude-code", None, None, presets=True).runner == "claude-code"


def test_summary_offers_the_presets_that_arent_set_up() -> None:
    s = ai.summary(Config.from_dict({"harnesses": {"codex": {"kind": "codex"}}}))
    assert [h["name"] for h in s["harnesses"]] == ["builtin", "codex"]
    presets = {p["name"]: p for p in s["presets"]}
    assert set(presets) == {"claude-code", "aider", "opencode", "hermes-agent"}
    assert presets["aider"]["model"] == "sonnet-api" and "installed" in presets["aider"]


def _pipeline(root: Path, name: str, body: str) -> Path:
    (root / name).mkdir(parents=True)
    p = root / name / "pipeline.yaml"
    p.write_text(f"version: 1\nname: {name}\n{body}")
    return p


def test_the_migration_keeps_what_was_in_use(tmp_path: Path) -> None:
    root = tmp_path / "pipelines"
    _pipeline(root, "uses-codex", "nodes:\n  a: { type: agent, harness: codex, prompt: hi }\n")
    old = _pipeline(
        root,
        "implicit",
        "# my pipeline\nnodes:\n  plan:  # the planner\n    type: agent\n    prompt: plan it\n",
    )
    _pipeline(root, "model-only", "nodes:\n  a: { type: agent, model: free, prompt: hi }\n")
    home = Path(os.environ["AGENTLINE_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "setup.json").write_text(json.dumps({"assistant": {"harness": "aider", "model": None}}))
    toml = tmp_path / "agentline.toml"
    toml.write_text('# mine\n[harnesses.codex]\nkind = "codex"\n')

    out = migrate(toml, root)
    assert out["done"] and out["harnesses"] == ["aider", "claude-code"]  # codex was configured
    tables = tomllib.loads(toml.read_text())["harnesses"]
    assert tables["aider"] == {"kind": "aider", "model": "sonnet-api"}
    assert tables["claude-code"]["kind"] == "claude-code"
    assert toml.read_text().startswith("# mine\n")
    # the node that relied on the old fallback keeps running on Claude Code
    text = old.read_text()
    assert "harness: claude-code" in text and "# the planner" in text and "# my pipeline" in text
    assert out["pipelines"] == ["implicit/pipeline.yaml"]
    assert "harness" not in (root / "model-only" / "pipeline.yaml").read_text()
    # once per data folder
    assert marker_path().exists() and migrate(toml, root)["done"] is False


def test_nothing_in_use_adds_nothing(tmp_path: Path) -> None:
    root = tmp_path / "pipelines"
    _pipeline(
        root, "scripts", "nodes:\n  a: { type: action, uses: shell.run, with: { command: ls } }\n"
    )
    toml = tmp_path / "agentline.toml"
    out = migrate(toml, root)
    assert out == {"done": True, "harnesses": [], "pipelines": []} and not toml.exists()


def test_setup_offers_an_installed_cli_and_adds_it_when_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from agentline import App, SqliteLedger
    from agentline.api.server import create_server

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text("#!/bin/sh\n")
    (bin_dir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    toml = tmp_path / "agentline.toml"
    monkeypatch.setenv("AGENTLINE_CONFIG", str(toml))
    (tmp_path / "p").mkdir()
    c = TestClient(
        create_server(App(SqliteLedger(tmp_path / "l.db")), [], pipelines_dir=tmp_path / "p")
    )
    s = c.get("/api/setup").json()
    cc = next(h for h in s["harnesses"] if h["id"] == "claude-code")
    assert cc["configured"] is False and cc["available"]
    assert "not set up yet" in cc["hint"]
    assert {h["id"] for h in s["harnesses"]} == {"builtin", "claude-code"}  # codex/aider: no CLI
    s = c.put("/api/setup/choice", json={"harness": "claude-code"}).json()
    assert s["choice"] == {"harness": "claude-code", "model": None}
    assert tomllib.loads(toml.read_text())["harnesses"]["claude-code"] == {"kind": "claude-code"}
    # the Chat can't be pointed at a harness that isn't set up
    r = c.put("/api/setup/choice", json={"harness": "codex"})
    assert r.status_code == 200  # codex is a preset: picking it sets it up (it needs no CLI here)
    assert "codex" in tomllib.loads(toml.read_text())["harnesses"]
    assert c.put("/api/setup/choice", json={"harness": "ghost"}).status_code == 422
