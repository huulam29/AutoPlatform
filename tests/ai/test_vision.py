"""Images for Chat agents that can't see them: a vision model transcribes once, the agent gets
the transcript, the user can correct or redo it (agentline.vision)."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger, vision
from agentline.api.server import create_server
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.registry import Limits, RunRequest, RunResult
from agentline.runners.function import FunctionRunner

PNG = b"\x89PNG\r\n\x1a\n" + b"1" * 40
TRANSCRIPT = (
    "TITLE: review loop\nSTEPS:\n- spec-writer (opus)\n- builder\nARROWS:\n- spec-writer -> builder"
)
REPLY = {"message": "Got it", "actions": []}


@pytest.fixture
def chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("shutil.which", lambda _b: None)  # no claude CLI
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    calls: list[RunRequest] = []

    async def fake(req: RunRequest) -> RunResult:
        calls.append(req)
        if req.node_id == "vision":
            return RunResult(output=f"{TRANSCRIPT} #{len(calls)}", cost_usd=0.002)
        return RunResult(output=REPLY, cost_usd=0.01)

    cfg = Config()
    reg = default_registry(cfg)
    reg.add_runner(FunctionRunner("llm", fake))  # the vision model's calls
    reg.add_runner(FunctionRunner("aider", fake))  # a Chat harness that can't see images
    reg.add_runner(FunctionRunner("claude-code", fake))
    server = create_server(
        App(SqliteLedger(tmp_path / "l.db")), [], pipelines_dir=pipelines, registry=reg, config=cfg
    )
    c = TestClient(server)
    att = c.post("/api/assistant/attachments", content=PNG, headers={"content-type": "image/png"})
    return c, calls, att.json()["id"]


def send(c: TestClient, att: str, agent: str = "aider") -> Any:
    msg = {"role": "user", "content": "build this", "attachments": [att]}
    return c.post("/api/assistant/chat", json={"agent": agent, "messages": [msg]})


def test_auto_picks_claude_code_then_an_anthropic_model(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config()
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/local/bin/claude")
    assert vision.effective(cfg) == "@claude-code"
    monkeypatch.setattr("shutil.which", lambda _b: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert vision.effective(cfg) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    picked = vision.effective(cfg)
    assert picked is not None and picked.endswith("-api")  # a built-in Anthropic API model
    vision.set_choice("free", cfg)  # an explicit model wins when it can run
    assert vision.choice() == "free"
    with pytest.raises(Exception, match="not a model name"):
        vision.set_choice("ghost", cfg)
    ids = [o["id"] for o in vision.options(cfg)]
    assert ids[:2] == ["@auto", "@claude-code"] and "free" in ids
    assert len(ids) == len(set(ids))  # a model named "auto" doesn't clash with @auto
    vision.set_choice("auto", cfg)  # that model, not the automatic pick
    assert vision.choice() == "auto"


def test_a_text_agent_gets_the_transcript_once_and_the_card_data(chat: Any) -> None:
    c, calls, att = chat
    r = send(c, att)
    assert r.status_code == 200, r.text
    vis, turn = calls
    assert vis.node_id == "vision" and vis.params["images"] == [
        {"media_type": "image/png", "data": base64.b64encode(PNG).decode()}
    ]
    assert "Never guess a label" in vis.prompt
    assert TRANSCRIPT in turn.prompt and "You can't see it" in turn.prompt
    (card,) = r.json()["transcripts"]
    assert card["id"] == att and card["cached"] is False and card["by"].endswith("-api")
    assert r.json()["cost_usd"] == pytest.approx(0.012)  # the transcription counts too
    r = send(c, att)  # cached: no second vision call
    assert len(calls) == 3 and r.json()["transcripts"][0]["cached"] is True


def test_the_users_correction_wins_and_redo_reads_again(chat: Any) -> None:
    c, calls, att = chat
    send(c, att)
    fixed = c.put(f"/api/assistant/transcripts/{att}", json={"text": "STEPS:\n- a\n- b"})
    assert fixed.status_code == 200 and fixed.json()["edited"] is True
    send(c, att)
    assert "STEPS:\n- a\n- b" in calls[-1].prompt and "the user (edited)" in calls[-1].prompt
    redo = c.post(f"/api/assistant/transcripts/{att}/redo").json()
    assert redo["edited"] is False and redo["cached"] is False and calls[-1].node_id == "vision"
    assert c.put("/api/assistant/transcripts/nope.png", json={"text": "x"}).status_code == 404
    assert c.put(f"/api/assistant/transcripts/{att}", json={"text": " "}).status_code == 422


def test_without_a_vision_model_the_chat_says_how_to_set_one(
    chat: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, calls, att = chat
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    r = send(c, att)
    assert r.status_code == 422 and "Images" in r.text and calls == []


def test_claude_code_reads_the_image_from_its_own_folder(
    chat: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, calls, att = chat
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/local/bin/claude")
    assert send(c, att).status_code == 200
    vis = calls[0]
    assert send(c, att).json()["transcripts"][0]["by"] == "Claude Code"
    ws = Path(vis.workspace.path)
    assert (ws / att).read_bytes() == PNG and f"Open {ws / att}" in vis.prompt
    assert vis.limits.allowed_tools == ("read",)


def test_api_runners_attach_images(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentline.runners import llm
    from agentline.runners.anthropic import _content

    img = [{"media_type": "image/png", "data": "QUJD"}]
    blocks = _content("describe", img)
    assert blocks[0]["source"] == {"type": "base64", "media_type": "image/png", "data": "QUJD"}
    assert blocks[-1] == {"type": "text", "text": "describe"} and _content("x", None) == "x"

    sent: dict[str, Any] = {}

    def post(url: str, body: Any, headers: Any, timeout: float) -> Any:
        sent.update(body)
        return {"choices": [{"message": {"content": "TITLE: x"}}], "usage": {}}

    monkeypatch.setattr(llm, "post_json", post)
    req = RunRequest(
        prompt="describe",
        inputs={},
        limits=Limits(),
        params={"base_url": "http://x/v1", "model": "m", "images": img, "price_unknown_ok": True},
    )
    res = asyncio.run(llm.LLMRunner().run(req))
    parts = sent["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "describe"}
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}
    assert res.output == "TITLE: x" and "QUJD" not in res.transcript
