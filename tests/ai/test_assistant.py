"""Editor assistant: context, reply parsing, checked proposals, one repair round, apply."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api.server import create_server
from agentline.assistant import parse_reply
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.registry import RunRequest, RunResult
from agentline.runners.function import FunctionRunner

GOOD = """\
version: 1
name: dev-cycle
nodes:
  plan:
    type: agent
    prompt: prompts/plan.md
    out: plan
    skills: [planning-and-task-breakdown]
  approve: { type: gate, prompt: "Ship {{plan.summary}}?", in: [plan] }
edges:
  - plan -> approve
"""
BAD = GOOD.replace("in: [plan]", "in: []")  # template uses plan without declaring it


@pytest.fixture
def setup(tmp_path: Path) -> tuple[TestClient, list[RunRequest], list[Any], Path]:
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "old.yaml").write_text("version: 1\nname: old\nnodes: {}\n")
    seen: list[RunRequest] = []
    replies: list[Any] = []

    async def fake(req: RunRequest) -> RunResult:
        seen.append(req)
        return RunResult(output=replies.pop(0), cost_usd=0.01)

    cfg = Config()
    reg = default_registry(cfg)
    reg.add_runner(FunctionRunner("claude-code", fake))
    app = App(SqliteLedger(tmp_path / "l.db"))
    server = create_server(app, [], pipelines_dir=pipelines, registry=reg, config=cfg)
    return TestClient(server), seen, replies, pipelines


def test_parse_reply_accepts_dicts_text_and_fences() -> None:
    assert parse_reply({"message": "hi", "choices": ["a"] * 9})["choices"] == ["a"] * 5
    fenced = 'Sure:\n```json\n{"message": "x", "actions": [{"type": "key", "name": "K"}]}\n```'
    assert parse_reply(fenced)["actions"] == [{"type": "key", "name": "K"}]
    trailing = '{"message": "y"} hope that helps'
    assert parse_reply(trailing)["message"] == "y"
    assert parse_reply("plain words") == {"message": "plain words", "choices": [], "actions": []}


def test_chat_sends_context_and_checks_actions(setup: Any) -> None:
    c, seen, replies, _ = setup
    replies.append(
        {
            "message": "Let's set up OpenRouter.",
            "choices": ["Yes", "No"],
            "actions": [
                {"type": "setting", "section": "models", "name": "qwen",
                 "body": {"provider": "openrouter", "model": "qwen/qwen3"}},
                {"type": "setting", "section": "models", "name": "bad",
                 "body": {"provider": "ghost", "model": "x"}},
                {"type": "setting", "section": "providers", "name": "p",
                 "body": {"type": "anthropic", "api_key": "sk-1"}},
                {"type": "key", "name": "OPENROUTER_API_KEY", "value": "leaked?"},
                {"type": "fetch_skills"},
                {"type": "nonsense"},
            ],
        }
    )  # fmt: skip
    r = c.post(
        "/api/assistant/chat",
        json={
            "messages": [{"role": "user", "content": "set me up"}],
            "pipeline": "old",
            "source": "version: 1\nname: old-unsaved\nnodes: {}\n",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"] == "Let's set up OpenRouter." and body["choices"] == ["Yes", "No"]
    issues = [a["issues"] for a in body["actions"]]
    assert issues[0] == [] and "unknown model source" in issues[1][0]
    assert "keys never go" in issues[2][0]
    assert issues[3] == [] and "value" not in body["actions"][3]
    assert issues[4] == [] and "unknown action type" in issues[5][0]
    prompt = seen[0].prompt
    for part in (
        "## SETTINGS",
        "OPENROUTER_API_KEY: NOT SET",
        "## RULES",
        "## SKILLS",
        "old-unsaved",
        "ticket-to-eod",
        "USER: set me up",
        "jira.get_ticket",
    ):
        assert part in prompt, part  # fmt: skip
    assert seen[0].limits.allowed_tools == ("read", "Skill")  # skills are invocable
    assert c.post("/api/assistant/chat", json={"messages": []}).status_code == 422
    r = c.post(
        "/api/assistant/chat",
        json={"agent": "ghost", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 422 and "can't run" in r.text


def test_invalid_pipeline_gets_one_repair_round_then_applies(setup: Any) -> None:
    c, seen, replies, pipelines = setup
    files = {
        "prompts/plan.md": "Plan the change. Answer with JSON {summary}.",
        "scripts/lint.sh": "#!/bin/sh\necho '{}'\n",
    }
    proposal = {"type": "pipeline", "path": "dev-cycle/pipeline.yaml", "files": files}
    replies.append(json.dumps({"message": "v1", "actions": [{**proposal, "yaml": BAD}]}))
    replies.append({"message": "fixed", "actions": [{**proposal, "yaml": GOOD}]})
    r = c.post(
        "/api/assistant/chat",
        json={"messages": [{"role": "user", "content": "build a dev cycle"}]},
    ).json()
    assert r["repaired"] and r["message"] == "fixed" and r["cost_usd"] == pytest.approx(0.02)
    assert "doesn't validate" in seen[1].prompt and "plan" in seen[1].prompt
    (action,) = r["actions"]
    assert action["issues"] == [] and action["exists"] is False
    assert not (pipelines / "dev-cycle").exists()  # proposals never write
    a = c.post(
        "/api/assistant/apply",
        json={"path": action["path"], "yaml": action["yaml"], "files": action["files"]},
    )
    assert a.status_code == 200 and a.json() == {"name": "dev-cycle/pipeline", "issues": []}
    assert (pipelines / "dev-cycle/prompts/plan.md").read_text().startswith("Plan the change")
    assert (pipelines / "dev-cycle/scripts/lint.sh").stat().st_mode & 0o111  # executable
    assert "dev-cycle/pipeline" in [p["name"] for p in c.get("/api/pipelines").json()["pipelines"]]
    for bad in ("../x.yaml", "/etc/x.yaml", "a.txt"):
        assert c.post("/api/assistant/apply", json={"path": bad, "yaml": GOOD}).status_code == 422
    r = c.post(
        "/api/assistant/apply",
        json={"path": "ok.yaml", "yaml": GOOD, "files": {"../../escape.md": "x"}},
    )
    assert r.status_code == 422


def test_disconnect_cancels_the_turn(tmp_path: Path) -> None:
    import asyncio

    from fastapi import HTTPException

    from agentline.api.assistant import ChatBody, create_assistant_router
    from agentline.api.editor import PipelineFiles

    cancelled: list[bool] = []

    async def slow(req: RunRequest) -> RunResult:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return RunResult(output={})

    reg = default_registry(Config())
    reg.add_runner(FunctionRunner("claude-code", slow))
    (tmp_path / "p").mkdir()
    router = create_assistant_router(PipelineFiles(tmp_path / "p", reg, Config()))
    route = next(r for r in router.routes if getattr(r, "path", "") == "/assistant/chat")

    class Gone:
        async def is_disconnected(self) -> bool:
            return True

    body = ChatBody(messages=[{"role": "user", "content": "hi"}])
    with pytest.raises(HTTPException) as e:
        asyncio.run(route.endpoint(body, Gone()))  # type: ignore[attr-defined]
    assert e.value.status_code == 499 and cancelled == [True]


def test_image_attachments_reach_claude_code(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    c, seen, replies, _ = setup
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    r = c.post("/api/assistant/attachments", content=png, headers={"content-type": "image/png"})
    assert r.status_code == 200, r.text
    att = r.json()
    assert att["id"].endswith(".png") and Path(att["path"]).read_bytes() == png
    assert c.get(f"/api/assistant/attachments/{att['id']}").content == png
    assert c.get("/api/assistant/attachments/../../etc.png").status_code == 404
    bad = c.post("/api/assistant/attachments", content=b"x", headers={"content-type": "text/plain"})
    assert bad.status_code == 422
    replies.append({"message": "Got it", "actions": []})
    msg = {"role": "user", "content": "build this", "attachments": [att["id"]]}
    r = c.post("/api/assistant/chat", json={"messages": [msg]})
    assert r.status_code == 200, r.text
    # the image is copied into the folder Claude Code runs in: it can't read outside it
    ws = Path(seen[-1].workspace.path)
    staged = ws / "attachments" / att["id"]
    assert str(staged) in seen[-1].prompt and "Read tool" in seen[-1].prompt
    assert staged.read_bytes() == png and att["path"] not in seen[-1].prompt
    # an agent that can't see images needs a vision model to read it (test_vision.py)
    monkeypatch.setattr("shutil.which", lambda _b: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = c.post("/api/assistant/chat", json={"agent": "llm", "messages": [msg]})
    assert r.status_code == 422 and "no vision model" in r.text
