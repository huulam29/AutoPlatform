# ruff: noqa: E501  (the fake CLIs print one JSON event per line)
"""opencode and hermes runners against fake CLIs, and the params agents.py builds for them."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentline import agents
from agentline.config import Config, HarnessProfile, ModelProfile, Provider
from agentline.registry import Limits, RunRequest
from agentline.runners import HermesRunner, OpenCodeRunner, RunnerError
from agentline.runners import hermes as hermes_mod
from agentline.runners import opencode as opencode_mod

# Prints the event format of the real CLI; the mode is a `mode:<name>` word in the prompt.
FAKE = r"""
import json, os, re, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_AGENT_LOG")
if log:
    json.dump({"argv": args, "env": {k: os.environ[k] for k in os.environ
               if k.startswith(("OPENCODE_", "OPENAI_", "OPENROUTER_", "ANTHROPIC_", "AGENTLINE_"))}},
              open(log, "w"))
prompt = args[-1] if "--" in args else next((a[8:] for a in args if a.startswith("--query=")), "")
mode = (re.search(r"mode:(\w+)", prompt) or [None, "json"])[1]
KIND = "%s"
def emit(**ev): print(json.dumps(ev))
if KIND == "opencode":
    emit(type="step_start", sessionID="ses_1", part={})
    if mode == "error":
        emit(type="error", sessionID="ses_1", error={"name": "APIError", "data": {"message": "bad key"}})
        sys.exit(1)
    if mode == "missing":
        emit(type="tool_use", sessionID="ses_1", part={"state": {"status": "completed", "output": "sh: psql: command not found"}})
    emit(type="text", sessionID="ses_1", part={"text": "thinking..."})
    emit(type="text", sessionID="ses_1", part={"text": '{"ok": true}' if mode == "json" else "done"})
    emit(type="step_finish", sessionID="ses_1", part={"cost": 0.0125, "tokens": {"input": 1000, "output": 200, "reasoning": 0, "cache": {"read": 50, "write": 0}}})
else:
    print("session_id: s1", file=sys.stderr)
    emit(type="tool", output="ran ls")
    if mode == "error":
        emit(type="result", exit_code=1, text="", error="provider rejected the key", session_id="s1")
        sys.exit(1)
    emit(type="result", exit_code=0, text='{"ok": true}' if mode == "json" else "done", session_id="s1", usage={"input_tokens": 800, "output_tokens": 100})
"""


def fake(tmp_path: Path, kind: str) -> str:
    script = tmp_path / f"fake_{kind}.py"
    script.write_text(FAKE % kind)
    wrapper = tmp_path / kind
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_AGENT_LOG", str(path))
    monkeypatch.setenv("MY_ROUTER_KEY", "sk-test-not-real")
    return path


def req(prompt: str, params: dict[str, Any], tools: tuple[str, ...] = ("read",)) -> RunRequest:
    return RunRequest(
        prompt=prompt,
        inputs={},
        limits=Limits(allowed_tools=tools, max_turns=7),
        params={"env": ["FAKE_AGENT_LOG"], **params},
    )


def test_opencode_runs_parses_cost_and_denies_tools(tmp_path: Path, log: Path) -> None:
    r = asyncio.run(
        OpenCodeRunner().run(
            req(
                "-starts with a dash mode:json",
                {"bin": fake(tmp_path, "opencode"), "model": "anthropic/claude-sonnet-5-5"},
            )
        )
    )
    assert r.output == {"ok": True}
    assert r.cost_usd == pytest.approx(0.0125)
    assert r.meta["usage"]["input_tokens"] == 1000 and r.meta["session_id"] == "ses_1"
    seen = json.loads(log.read_text())
    assert seen["argv"][:5] == ["run", "--format", "json", "--model", "anthropic/claude-sonnet-5-5"]
    assert seen["argv"][-2:] == ["--", "-starts with a dash mode:json"]
    assert "--auto" not in seen["argv"]  # read-only: nothing to approve
    assert json.loads(seen["env"]["OPENCODE_PERMISSION"]) == {
        "edit": "deny", "bash": "deny", "webfetch": "deny"
    }  # fmt: skip


def test_opencode_edit_and_bash_auto_approve(tmp_path: Path, log: Path) -> None:
    asyncio.run(
        OpenCodeRunner().run(
            req("mode:text", {"bin": fake(tmp_path, "opencode")}, ("read", "edit", "bash", "web"))
        )
    )
    seen = json.loads(log.read_text())
    assert "--auto" in seen["argv"] and "OPENCODE_PERMISSION" not in seen["env"]


def test_opencode_errors_and_missing_tools(tmp_path: Path, log: Path) -> None:
    bin = fake(tmp_path, "opencode")
    with pytest.raises(RunnerError, match="bad key"):
        asyncio.run(OpenCodeRunner().run(req("mode:error", {"bin": bin})))
    r = asyncio.run(OpenCodeRunner().run(req("mode:missing", {"bin": bin})))
    assert r.missing_tools == ["psql"]


def test_hermes_runs_with_toolsets_and_price(tmp_path: Path, log: Path) -> None:
    params = {
        "bin": fake(tmp_path, "hermes"),
        "model": "m1",
        "provider": "custom",
        "base_url": "http://x/v1",
        "input_usd_per_mtok": 1.0,
        "output_usd_per_mtok": 10.0,
    }
    r = asyncio.run(HermesRunner().run(req("mode:json", params, ("read", "bash"))))
    assert r.output == {"ok": True}
    assert r.cost_usd == pytest.approx((800 * 1 + 100 * 10) / 1e6)
    argv = json.loads(log.read_text())["argv"]
    assert argv[:4] == ["chat", "--query=mode:json", "--oneshot", "--format"]
    assert (
        argv[argv.index("--provider") + 1] == "custom"
        and argv[argv.index("--base-url") + 1] == "http://x/v1"
    )
    assert argv[argv.index("--toolsets") + 1] == "file,terminal" and "--yolo" in argv
    assert argv[argv.index("--max-turns") + 1] == "7"


def test_hermes_read_only_has_no_terminal_and_reports_errors(tmp_path: Path, log: Path) -> None:
    bin = fake(tmp_path, "hermes")
    r = asyncio.run(HermesRunner().run(req("mode:text", {"bin": bin})))
    assert r.output == "done" and r.meta["warnings"]  # no price known: cost 0, said so
    argv = json.loads(log.read_text())["argv"]
    assert argv[argv.index("--toolsets") + 1] == "file" and "--yolo" not in argv
    with pytest.raises(RunnerError, match="provider rejected the key"):
        asyncio.run(HermesRunner().run(req("mode:error", {"bin": bin})))


def test_arg_builders() -> None:
    assert opencode_mod.permissions(("read", "edit")) == {"bash": "deny", "webfetch": "deny"}
    assert hermes_mod.toolsets(("web",)) == ["web"]
    custom = req("hi", {"args": ["x", "{prompt}"]})
    assert hermes_mod.build_argv(custom)[1:] == ["x", "hi"]


def _cfg() -> Config:
    cfg = Config()
    cfg.providers["homelab"] = Provider(
        name="homelab",
        type="openai-compatible",
        base_url="http://lab:8000/v1",
        api_key_env="LAB_KEY",
    )
    cfg.models["qwen"] = ModelProfile("qwen", "homelab", "qwen/qwen3")
    cfg.models["router"] = ModelProfile("router", "openrouter", "meta/llama")
    cfg.harnesses["opencode"] = HarnessProfile("opencode", "opencode")
    cfg.harnesses["hermes-agent"] = HarnessProfile("hermes-agent", "hermes")
    return cfg


def test_params_per_source() -> None:
    cfg = _cfg()
    oc = agents.resolve("opencode", "qwen", cfg).params
    assert oc["model"] == "agentline/qwen/qwen3"
    provider = json.loads(oc["env_set"]["OPENCODE_CONFIG_CONTENT"])["provider"]["agentline"]
    assert provider["options"] == {
        "baseURL": "http://lab:8000/v1",
        "apiKey": "{env:AGENTLINE_PROVIDER_KEY}",
    }
    assert oc["env_map"] == {"AGENTLINE_PROVIDER_KEY": "LAB_KEY"}
    assert (
        agents.resolve("opencode", "sonnet-api", cfg).params["model"]
        == "anthropic/claude-sonnet-5-5"
    )
    h = agents.resolve("hermes-agent", "qwen", cfg).params
    assert (h["provider"], h["base_url"], h["env_map"]) == (
        "custom",
        "http://lab:8000/v1",
        {"OPENAI_API_KEY": "LAB_KEY"},
    )
    h = agents.resolve("hermes-agent", "router", cfg).params
    assert (h["provider"], h["env_map"]) == (
        "openrouter",
        {"OPENROUTER_API_KEY": "OPENROUTER_API_KEY"},
    )
    # no model: the CLI's own default, with the usual keys passed
    assert "ANTHROPIC_API_KEY" in agents.resolve("hermes-agent", None, cfg).params.get(
        "env", ["ANTHROPIC_API_KEY"]
    )


@pytest.mark.builtin_only
def test_presets_offered_and_subscription_refused() -> None:
    names = {p["name"]: p for p in agents.summary(Config())["presets"]}
    assert names["opencode"]["kind"] == "opencode" and names["hermes-agent"]["kind"] == "hermes"
    with pytest.raises(Exception, match="subscription"):
        agents.resolve("opencode", "opus", _cfg())
