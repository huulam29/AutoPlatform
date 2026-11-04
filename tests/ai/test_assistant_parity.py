"""Chat parity: every GUI action has a Chat action, checked against config + LIVE STATE."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api.server import create_server
from agentline.assistant import ACTION_TYPES, Live, check_actions, live_text, title
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.registry import RunRequest, RunResult
from agentline.runners.function import FunctionRunner

CFG = Config.from_dict(
    {
        "providers": {"or": {"type": "openai-compatible", "base_url": "http://x/v1"}},
        "models": {"cheap": {"provider": "or", "model": "a/b"}},
        "agents": {"helper": {"kind": "llm", "model": "cheap"}},  # old names still read
        "mcp": {"gh": {"command": "npx"}},
    }
)
LIVE = Live(
    flows=["dev-cycle", "helpdesk"],
    starts={"dev-cycle": "json: key", "helpdesk": "chat"},
    runs=[{"id": "r1", "flow": "dev-cycle", "status": "waiting"}],
    gates=[{"run_id": "r1", "node_id": "approve", "prompt": "Ship?"}],
    issues=[{"number": 3, "status": "open", "title": "boom"}],
    incidents=["inc-1"],
    budget=20.0,
    chat={"telegram": {"linked": False, "keys_set": True}, "notify": {"gates": True}},
    history={"dev": [{"commit": "abcdef1234567890", "message": "edit"}]},
    detected=[
        {
            "id": "claude-code:github",
            "name": "github",
            "source": "Claude Code",
            "transport": "stdio",
            "status": "new",
            "secrets": [{"key": "GH", "from": "env GH"}],
        },
        {
            "id": "codex:old",
            "name": "old",
            "source": "Codex",
            "transport": "stdio",
            "status": "configured",
            "secrets": [],
        },
    ],
    own_skills=["our-review"],
    triage=[
        {
            "id": "t0123456789ab",
            "run": "r1",
            "node": "build",
            "verdict": "infrastructure",
            "status": "done",
            "summary": "lost result",
            "github": False,
        },
    ],
    reviews=[
        {
            "id": "v0123456789ab",
            "run": "r1",
            "flow": "dev-cycle",
            "status": "done",
            "summary": "two review loops",
            "proposals": [{"index": 0, "type": "pipeline", "title": "update", "probe": None}],
        }
    ],
    conversations=[
        {"id": "c-active1", "title": "Build a pipeline", "updated_at": 0, "archived": False},
        {"id": "c-old0001", "title": "Old chat", "updated_at": 0, "archived": True},
    ],
)

# (action, expected issue substring or "" for valid)
CASES: list[tuple[dict[str, Any], str]] = [
    ({"type": "delete_setting", "section": "harnesses", "name": "helper"}, ""),
    ({"type": "delete_setting", "section": "agents", "name": "helper"}, ""),  # old section name
    ({"type": "delete_setting", "section": "models", "name": "cheap"}, "default of harnesses"),
    ({"type": "delete_setting", "section": "providers", "name": "or"}, "used by models"),
    (
        {"type": "delete_setting", "section": "providers", "name": "or", "with_models": True},
        "defaults of harnesses",
    ),
    ({"type": "delete_setting", "section": "providers", "name": "openai"}, ""),  # built in
    ({"type": "delete_setting", "section": "providers", "name": "ghost"}, "no model source"),
    ({"type": "claude_logout", "provider": "claude"}, ""),
    ({"type": "claude_logout", "provider": "or"}, "isn't a Claude subscription"),
    ({"type": "delete_setting", "section": "harnesses", "name": "claude-code"}, "built-ins"),
    ({"type": "delete_setting", "section": "mcp", "name": "gh"}, ""),
    (
        {
            "type": "setting",
            "section": "mcp",
            "name": "gh",
            "body": {"command": "npx", "enabled": False},
        },
        "",
    ),
    ({"type": "source_models", "provider": "or", "models": ["a/b", "c/d"]}, ""),
    ({"type": "source_models", "provider": "ghost", "models": []}, "unknown model source"),
    ({"type": "model_visibility", "model": "cheap", "hidden": True}, ""),
    ({"type": "model_visibility", "model": "ghost", "hidden": True}, "unknown model"),
    ({"type": "model_visibility", "model": "cheap", "hidden": "yes"}, "true or false"),
    ({"type": "delete_key", "name": "OPENROUTER_API_KEY"}, ""),
    ({"type": "delete_key", "name": "bad name"}, "bad key name"),
    ({"type": "chat_uses", "harness": "helper"}, ""),
    ({"type": "chat_uses", "agent": "helper"}, ""),  # the old field name
    ({"type": "chat_uses", "harness": "llm"}, ""),  # the old name of `builtin`
    ({"type": "chat_instructions", "text": "- Be brief."}, ""),
    ({"type": "chat_uses", "harness": "claude-code", "model": "cheap"}, "Anthropic"),
    ({"type": "chat_uses", "harness": "ghost"}, "unknown harness"),
    ({"type": "delete_skill", "name": "our-review"}, ""),
    ({"type": "delete_skill", "name": "code-review"}, "installed skills"),
    ({"type": "install_skills", "source": "docker/skills"}, ""),
    ({"type": "install_skills", "source": "npx skills add docker/skills", "names": ["a-b"]}, ""),
    ({"type": "install_skills", "source": "docker"}, "owner/repo"),
    ({"type": "install_skills", "source": "docker/skills", "names": []}, "names must"),
    ({"type": "mcp_import", "items": [{"id": "claude-code:github", "name": "gh2"}]}, ""),
    ({"type": "mcp_import", "items": [{"id": "codex:old"}]}, "not a detected new"),
    ({"type": "mcp_import", "items": []}, "items must"),
    ({"type": "chat_app", "platform": "telegram", "op": "pair"}, ""),
    ({"type": "chat_app", "platform": "icq", "op": "pair"}, "unknown chat app"),
    ({"type": "chat_app", "platform": "slack", "op": "nuke"}, "op must"),
    ({"type": "chat_notify", "gates": False, "done": True}, ""),
    ({"type": "chat_notify", "gates": "yes"}, "true or false"),
    ({"type": "engine", "op": "restart"}, ""),
    ({"type": "engine", "op": "format-disk"}, "op must"),
    ({"type": "engine", "op": "choose_folder", "path": "/srv/pipelines"}, ""),
    ({"type": "engine", "op": "choose_folder", "path": "  "}, "path must"),
    ({"type": "engine", "op": "reveal", "target": "pipelines"}, ""),
    ({"type": "engine", "op": "reveal", "target": "/etc"}, "target must"),
    ({"type": "engine", "op": "export_reports"}, ""),
    ({"type": "export", "pipeline": "dev"}, ""),
    ({"type": "export", "pipeline": "../etc/passwd"}, "bad pipeline name"),
    ({"type": "export", "pipeline": "ghost"}, "no pipeline file"),
    ({"type": "import", "folder": "imported", "overwrite": False}, ""),
    ({"type": "import", "folder": "../x"}, "bad folder"),
    (
        {"type": "dry_run", "pipeline": "dev.yaml", "payload": {"k": 1}, "routes": {"r": ["a"]}},
        "",
    ),
    ({"type": "dry_run", "pipeline": "dev", "routes": ["a"]}, "routes must"),
    ({"type": "restore", "pipeline": "dev", "commit": "abcdef12"}, ""),
    ({"type": "restore", "pipeline": "dev", "commit": "1234567"}, "isn't in the history"),
    ({"type": "restore", "pipeline": "dev", "commit": "HEAD~1"}, "bad commit"),
    ({"type": "delete_pipeline", "pipeline": "dev"}, ""),
    ({"type": "chat_vision", "choice": "@auto"}, ""),
    ({"type": "chat_vision", "choice": "auto"}, ""),  # the built-in model named "auto"
    ({"type": "chat_vision", "choice": "nope"}, "choice must be"),
    ({"type": "delete_pipeline", "pipeline": "nope"}, "nope"),
    ({"type": "start_run", "pipeline": "dev-cycle", "payload": {"key": "T-1"}}, ""),
    ({"type": "start_run", "pipeline": "dev"}, "no loaded pipeline"),
    ({"type": "start_run", "pipeline": "helpdesk", "text": "fix login"}, ""),
    ({"type": "start_run", "pipeline": "helpdesk", "payload": {}}, "starts from chat: give 'text'"),
    ({"type": "run_message", "run": "r1", "text": "retry qa"}, ""),
    ({"type": "run_message", "run": "r1", "text": " "}, "what's the message?"),
    ({"type": "retry", "run": "r1", "node": "plan"}, ""),
    ({"type": "gate", "run": "r1", "node": "approve", "decision": "approve"}, ""),
    ({"type": "gate", "run": "r1", "node": "approve", "decision": "maybe"}, "approve or reject"),
    ({"type": "gate", "run": "r2", "node": "approve", "decision": "reject"}, "no gate"),
    ({"type": "issue", "number": 3, "status": "in_progress", "note": "on it"}, ""),
    ({"type": "issue", "number": 3, "note": "just a note"}, ""),
    ({"type": "issue", "number": 3, "status": "done"}, "status must"),
    ({"type": "issue", "number": 9, "status": "fixed"}, "no issue #9"),
    ({"type": "issue", "number": 3}, "status, add a note"),
    ({"type": "incident", "op": "delete", "id": "inc-1"}, ""),
    ({"type": "incident", "op": "delete", "id": "inc-9"}, "no crash report"),
    ({"type": "incident", "op": "clear"}, ""),
    ({"type": "incident", "op": "seen", "id": "inc-1"}, ""),
    ({"type": "incident", "op": "seen", "id": "inc-9"}, "no crash report"),
    ({"type": "incident", "op": "burn", "id": "inc-1"}, "op must"),
    ({"type": "budget", "monthly_usd": 50}, ""),
    ({"type": "budget", "monthly_usd": None}, ""),
    ({"type": "budget", "monthly_usd": -5}, "number of dollars"),
    ({"type": "triage", "run": "r1", "node": "build"}, ""),
    ({"type": "triage", "run": "r 1"}, "bad run id"),
    ({"type": "lesson", "text": "Give planners a schema."}, ""),
    ({"type": "lesson", "text": "x" * 400}, "one line"),
    ({"type": "report_issue", "triage": "t0123456789ab"}, ""),
    ({"type": "report_issue", "triage": "t0123456789ff"}, "no triage"),
    ({"type": "github", "repo": "git@github.com:o/r.git"}, ""),
    ({"type": "github", "op": "save", "auto": True}, ""),
    ({"type": "github", "op": "save"}, "give the repo"),
    ({"type": "github", "repo": "nope"}, "owner/name"),
    ({"type": "github", "op": "login"}, ""),
    ({"type": "github", "op": "test"}, ""),
    ({"type": "github", "op": "push"}, "op must be"),
    ({"type": "conversation", "op": "archive", "id": "c-active1"}, ""),
    ({"type": "conversation", "op": "unarchive", "id": "c-old0001"}, ""),
    ({"type": "conversation", "op": "archive", "id": "c-old0001"}, "already archived"),
    ({"type": "conversation", "op": "archive", "id": "c-ghost01"}, "no conversation"),
    ({"type": "conversation", "op": "delete", "id": "c-active1"}, "op must be"),
    ({"type": "review", "run": "r1"}, ""),
    ({"type": "review", "run": "r 1"}, "bad run id"),
    ({"type": "probe", "review": "v0123456789ab", "index": 0}, ""),
    ({"type": "probe", "review": "v0123456789ab", "index": 3}, "isn't a pipeline edit"),
    ({"type": "probe", "review": "v0123456789ff", "index": 0}, "no review"),
    (
        {
            "type": "pipeline_lesson",
            "pipeline": "dev",
            "node": "plan",
            "cue": "when x",
            "lesson": "do y",
            "confidence": "low",
        },
        "",
    ),
    ({"type": "pipeline_lesson", "pipeline": "dev", "node": "plan", "cue": ""}, "needs a cue"),
    (
        {"type": "pipeline_lesson", "pipeline": "ghost", "node": "p", "cue": "c", "lesson": "l"},
        "no pipeline file",
    ),
]


@pytest.mark.parametrize("action,expect", CASES, ids=[f"{a['type']}-{i}" for i, (a, _) in
                                                     enumerate(CASES)])  # fmt: skip
def test_each_action_is_checked(tmp_path: Path, action: dict[str, Any], expect: str) -> None:
    (tmp_path / "dev.yaml").write_text("version: 1\nname: dev-cycle\nnodes: {}\n")
    [out] = check_actions([dict(action)], tmp_path, None, CFG, LIVE)
    if expect:
        assert out["issues"] and expect in out["issues"][0], out["issues"]
    else:
        assert out["issues"] == [], out["issues"]


def test_every_type_has_a_case_and_a_title() -> None:
    covered = {a["type"] for a, _ in CASES} | {"key", "fetch_skills", "skill", "pipeline"}
    assert covered == set(ACTION_TYPES)
    for t in ACTION_TYPES:
        assert title({"type": t}) != t or t == "import", t


def test_key_actions_never_carry_a_value(tmp_path: Path) -> None:
    for t in ("key", "delete_key"):
        [out] = check_actions([{"type": t, "name": "K", "value": "sk-x"}], tmp_path, None, CFG)
        assert "value" not in out


def test_unknown_live_state_isnt_checked(tmp_path: Path) -> None:
    """Without an engine behind the call (Live() all unknown), only the shape is checked."""
    [out] = check_actions(
        [{"type": "gate", "run": "r9", "node": "g", "decision": "approve"}], tmp_path, None, CFG
    )
    assert out["issues"] == []


def test_live_text_names_everything_the_agent_proposes_against() -> None:
    text = live_text(LIVE)
    for part in (
        "LOADED pipelines (start_run, with their start input): dev-cycle (json: key), helpdesk"
        " (chat)",
        "r1: dev-cycle waiting",
        "run r1 node approve: Ship?",
        "#3 [open] boom",
        "Crash reports: inc-1",
        "Monthly budget: $20",
        "telegram: not linked",
        "HISTORY of dev",
        "abcdef1234",
        "claude-code:github: github (Claude Code, stdio, keys GH come with it)",
        "Your own and installed skills: our-review",
        "t0123456789ab: run r1 node build: infrastructure lost result",
    ):
        assert part in text, part
    assert "codex:old" not in text  # already configured


def test_chat_api_sends_live_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "dev.yaml").write_text("version: 1\nname: dev-cycle\nnodes: {}\n")
    seen: list[RunRequest] = []
    replies: list[Any] = [
        {
            "message": "ok",
            "actions": [{"type": "gate", "run": "r9", "node": "g", "decision": "approve"}],
        },
        {"message": "there's no such gate", "actions": []},
    ]

    async def fake(req: RunRequest) -> RunResult:
        seen.append(req)
        return RunResult(output=replies.pop(0), cost_usd=0.0)

    cfg = Config()
    reg = default_registry(cfg)
    reg.add_runner(FunctionRunner("claude-code", fake))
    app = App(SqliteLedger(tmp_path / "l.db"))
    c = TestClient(create_server(app, [], pipelines_dir=pipelines, registry=reg, config=cfg))
    assert c.put("/api/budget", json={"monthly_usd": 12}).status_code == 200
    r = c.post(
        "/api/assistant/chat",
        json={"messages": [{"role": "user", "content": "approve it"}], "pipeline": "dev"},
    )
    assert r.status_code == 200, r.text
    prompt = seen[0].prompt
    assert "## LIVE STATE" in prompt and "Monthly budget: $12" in prompt
    assert "Gates waiting for a decision:\n  none" in prompt
    # a gate that isn't waiting gets one repair round; the agent's answer without it stands
    assert len(seen) == 2 and "no gate 'g' waiting in run r9" in seen[1].prompt
    assert r.json()["actions"][0]["issues"]
