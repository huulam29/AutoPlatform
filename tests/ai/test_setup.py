"""First-run setup: which LLM routes work, the assistant's saved choice, the popup flag."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api.server import create_server
from agentline.config import Config


@pytest.fixture
def c(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))  # no claude CLI
    cfg = Config.from_dict(
        {
            "providers": {
                "ollama": {"type": "openai-compatible", "base_url": "http://127.0.0.1:9/v1"}
            }
        }
    )
    (tmp_path / "pipelines").mkdir()
    app = App(SqliteLedger(tmp_path / "l.db"))
    return TestClient(create_server(app, [], pipelines_dir=tmp_path / "pipelines", config=cfg))


def test_nothing_works_then_a_key_makes_it_ready(
    c: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = c.get("/api/setup").json()
    assert s["needs_setup"] and s["first_run"] and not s["ready"]
    # step 1: model sources
    sources = {x["id"]: x for x in s["sources"]}
    assert list(sources)[:5] == ["openrouter", "anthropic", "openai", "claude", "ollama"]
    assert not any(x["available"] for x in s["sources"])
    assert sources["openrouter"]["key"] == "OPENROUTER_API_KEY"
    assert "Install Claude Code" in sources["claude"]["hint"]
    assert sources["ollama"]["key"] is None and "not running" in sources["ollama"]["hint"]
    # step 2: harnesses
    harnesses = {h["id"]: h for h in s["harnesses"]}
    assert list(harnesses) == [
        "claude-code", "builtin", "codex", "aider", "opencode", "hermes-agent"
    ]  # fmt: skip
    assert not any(h["available"] for h in s["harnesses"])
    assert "isn't installed" in harnesses["claude-code"]["hint"]
    assert "step 1" in harnesses["builtin"]["hint"]
    assert s["assistant"] is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    s = c.get("/api/setup").json()
    assert s["ready"] and s["needs_setup"]  # first run: still shown once
    builtin = next(h for h in s["harnesses"] if h["id"] == "builtin")
    # only models whose source works, the preferred first; none from the missing Anthropic key
    assert builtin["available"] and builtin["models"][:2] == ["free", "auto"]
    assert "sonnet-api" not in builtin["models"] and builtin["model"] == "free"
    assert s["assistant"] == {"harness": "builtin", "model": "free"}
    s = c.put("/api/setup/choice", json={"harness": "builtin", "model": "free"}).json()
    assert not s["needs_setup"] and s["choice"] == {"harness": "builtin", "model": "free"}
    assert c.put("/api/setup/choice", json={"harness": "ghost"}).status_code == 422
    # the old field and harness names are still taken
    s = c.put("/api/setup/choice", json={"agent": "llm", "model": "free"}).json()
    assert s["choice"] == {"harness": "builtin", "model": "free"}
    # the key goes away: the popup comes back
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert c.get("/api/setup").json()["needs_setup"]


def test_claude_cli_counts_and_dismiss(c: TestClient, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text("#!/bin/sh\n")
    (bin_dir / "claude").chmod(0o755)
    s = c.get("/api/setup").json()
    assert s["ready"] and s["assistant"] == {"harness": "claude-code", "model": None}
    assert next(x for x in s["sources"] if x["id"] == "claude")["available"]
    cc = next(h for h in s["harnesses"] if h["id"] == "claude-code")
    assert cc["available"] and "opus" in cc["models"]  # the subscription's models
    s = c.post("/api/setup/dismiss").json()
    assert not s["first_run"] and not s["needs_setup"]


def test_a_running_local_server_gives_the_builtin_harness_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentline import setup

    from .fakes import FakeLLM, fake_llm

    for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with fake_llm(FakeLLM(require_key=None)) as (base, _):
        cfg = Config.from_dict(
            {"providers": {"ollama": {"type": "openai-compatible", "base_url": base}}}
        )
        s = setup.status(cfg)
    ollama = next(x for x in s["sources"] if x["id"] == "ollama")
    first = ollama["local_models"][0]
    assert ollama["available"] and "ollama:openai/gpt-big" in ollama["local_models"]
    builtin = next(h for h in s["harnesses"] if h["id"] == "builtin")
    assert builtin["available"] and builtin["model"] == first
    assert s["assistant"] == {"harness": "builtin", "model": first}
    assert setup.status(cfg, check_local=False)["sources"][4]["local_models"] == []
