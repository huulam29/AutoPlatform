"""A provider's key reaches the agent CLI under the name the CLI expects, and nothing else."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from agentline.registry import Limits, RunRequest
from agentline.runners.claude_code import ClaudeCodeRunner
from tests.fakebin import fake_bin

FAKE = Path(__file__).resolve().parents[1] / "runners" / "fake_claude.py"


def run(tmp_path: Path, params: dict[str, object]) -> dict[str, object]:
    log = tmp_path / "argv.json"
    wrapper = fake_bin(tmp_path, "claude", script=FAKE)
    p = {"bin": str(wrapper), **params}
    p["env"] = [*list(p.get("env") or []), "FAKE_CLAUDE_LOG"]  # type: ignore[call-overload]
    os.environ["FAKE_CLAUDE_LOG"] = str(log)
    try:
        req = RunRequest(prompt="hi", inputs={}, limits=Limits(), params=p)
        asyncio.run(ClaudeCodeRunner().run(req))
    finally:
        os.environ.pop("FAKE_CLAUDE_LOG", None)
    return json.loads(log.read_text())  # type: ignore[no-any-return]


def test_api_key_provider_is_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORK_ANTHROPIC_KEY", "sk-ant-work-123456789")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-personal-123456789")
    seen = run(tmp_path, {"env": [], "env_map": {"ANTHROPIC_API_KEY": "WORK_ANTHROPIC_KEY"}})
    assert seen["has_api_key"] is True
    assert "WORK_ANTHROPIC_KEY" not in seen["env_keys"]


def test_subscription_passes_no_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-personal-123456789")
    seen = run(tmp_path, {"env": []})
    assert seen["has_api_key"] is False


def test_env_set_literal(tmp_path: Path) -> None:
    seen = run(tmp_path, {"env": [], "env_set": {"ANTHROPIC_BASE_URL": "https://gw.test"}})
    assert "ANTHROPIC_BASE_URL" in seen["env_keys"]
