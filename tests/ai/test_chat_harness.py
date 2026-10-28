"""The built-in Chat harness (chat_harness.py): a Pydantic AI tool loop against a fake
OpenAI-compatible server, and its App reports for what it can't handle."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import RunContext

from agentline import App, SqliteLedger, chat_harness
from agentline.api.server import create_server
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.incidents import IncidentStore

from .conftest import OPENROUTER_TOML
from .fakes import KEY, FakeLLM, fake_llm, tool_calls

GOOD = """\
version: 1
name: lint
nodes:
  lint: { type: action, uses: shell.run, with: { command: "make lint" } }
"""


def reply(message: str, actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return tool_calls(("reply", {"message": message, "choices": [], "actions": actions or []}))


@pytest.fixture
def chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, FakeLLM, Path]]:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    chat_harness._NO_TOOLS.clear()
    pipelines = tmp_path / "pipelines"
    (pipelines / "dev").mkdir(parents=True)
    (pipelines / "dev" / "pipeline.yaml").write_text(GOOD)
    (pipelines / "dev" / "prompts").mkdir()
    (pipelines / "dev" / "prompts" / "plan.md").write_text("SECRET-PROMPT-TEXT plan it")
    st = FakeLLM()
    with fake_llm(st) as (base, _):
        cfg = Config.from_dict(tomllib.loads(OPENROUTER_TOML.format(base=base)))
        app = App(SqliteLedger(tmp_path / "l.db"))
        server = create_server(
            app, [], pipelines_dir=pipelines, registry=default_registry(cfg), config=cfg
        )
        yield TestClient(server), st, tmp_path


def ask(c: TestClient, text: str, **kw: Any) -> Any:
    body = {"harness": "builtin", "model": "cheap", "messages": [{"role": "user", "content": text}]}
    return c.post("/api/assistant/chat", json={**body, **kw})


def incidents() -> list[dict[str, Any]]:
    store = IncidentStore()
    return [store.get(i["id"]) for i in store.list(100) if i.get("kind") == "chat-harness"]


def test_lookup_then_reply(chat: Any) -> None:
    c, st, _ = chat
    st.replies += [
        tool_calls(("read_pipeline_file", {"path": "dev/prompts/plan.md"})),
        reply("Here is a lint pipeline.", [
            {"type": "pipeline", "path": "lint/pipeline.yaml", "yaml": GOOD, "summary": "lint"}
        ]),
    ]  # fmt: skip
    r = ask(c, "build a lint pipeline")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"] == "Here is a lint pipeline."
    assert body["actions"][0]["type"] == "pipeline" and body["actions"][0]["issues"] == []
    assert body["steps"] == ["read dev/prompts/plan.md"] and body["report"] is None
    assert body["cost_usd"] == 0  # a :free model
    first, second = st.requests
    assert {t["function"]["name"] for t in first["tools"]} >= {"reply", "read_pipeline_file"}
    assert "SETTINGS" in first["messages"][0]["content"]  # the guide + context as instructions
    assert first["max_tokens"] == chat_harness.CHAT_MAX_TOKENS
    tool_msg = next(m for m in second["messages"] if m["role"] == "tool")
    assert "SECRET-PROMPT-TEXT" in tool_msg["content"]
    assert incidents() == []


def test_invalid_actions_go_back_once_then_are_reported(chat: Any) -> None:
    c, st, _ = chat
    bad = {"type": "pipeline", "path": "x/pipeline.yaml", "yaml": "nodes: [", "summary": "x"}
    st.replies += [reply("try 1", [bad]), reply("try 2", [bad])]
    body = ask(c, "build it").json()
    retry = st.requests[1]["messages"][-1]
    assert retry["role"] == "tool" and "x/pipeline.yaml" in retry["content"]
    assert body["message"] == "try 2" and body["actions"][0]["issues"]
    assert body["report"]["class"] == "invalid"
    (inc,) = incidents()
    assert inc["context"]["class"] == "invalid" and "nodes: [" not in json.dumps(inc)


def test_a_retry_that_doesnt_parse_gets_one_more_try(chat: Any) -> None:
    c, st, _ = chat
    bad = {"type": "pipeline", "path": "x/pipeline.yaml", "yaml": "nodes: [", "summary": "x"}
    good = {"type": "pipeline", "path": "lint/pipeline.yaml", "yaml": GOOD, "summary": "lint"}
    st.replies += [reply("try 1", [bad]), "no tool call this time", reply("fixed", [good])]
    body = ask(c, "build it").json()
    assert body["message"] == "fixed" and body["actions"][0]["issues"] == []
    assert body["report"] is None and incidents() == []


def test_a_retry_that_never_parses_keeps_the_checked_reply(chat: Any) -> None:
    # a free model answered with a pipeline that failed a check, then never called `reply`
    # again: the user gets the first reply with its issues, reported as invalid (not unparseable)
    c, st, _ = chat
    bad = {"type": "pipeline", "path": "x/pipeline.yaml", "yaml": "nodes: [", "summary": "x"}
    st.replies += [reply("try 1", [bad]), "plain text", "more plain text"]
    body = ask(c, "build it").json()
    assert body["message"] == "try 1" and body["actions"][0]["issues"]
    assert body["report"]["class"] == "invalid"
    (inc,) = incidents()
    assert inc["context"]["class"] == "invalid"


def test_a_provider_error_mid_reply_is_not_reported(chat: Any) -> None:
    # OpenRouter answers 200 with finish_reason "error" when the upstream model fails
    c, st, _ = chat
    st.replies.append({"content": "", "finish_reason": "error"})
    r = ask(c, "hi")
    assert r.status_code == 502 and "provider failed" in r.json()["detail"]
    assert incidents() == []


def test_cant_do_is_a_gap_report_without_the_conversation(chat: Any) -> None:
    c, st, _ = chat
    st.replies += [
        tool_calls(("cant_do", {"what": "schedule a Zoom call", "why": "no calendar action"})),
        reply("I can't schedule calls yet."),
    ]
    body = ask(c, "please book a zoom call with PRIVATE-NAME").json()
    assert body["report"]["class"] == "gap" and body["message"].startswith("I can't")
    (inc,) = incidents()
    assert "schedule a Zoom call" in inc["message"] and "PRIVATE-NAME" not in json.dumps(inc)
    # the same gap again counts up the same report (one GitHub issue, not two)
    st.replies += [
        tool_calls(("cant_do", {"what": "schedule a Zoom call", "why": "no calendar action"})),
        reply("Still can't."),
    ]
    ask(c, "zoom again")
    (inc,) = incidents()
    assert inc["count"] == 2


def test_the_same_lookup_three_times_is_a_loop(chat: Any) -> None:
    c, st, _ = chat
    look = tool_calls(("engine_logs", {"limit": 10}))
    st.replies += [look, look, look, reply("done looking")]
    body = ask(c, "what happened?").json()
    assert body["message"] == "done looking" and body["report"]["class"] == "loop"
    assert "Answer now" in st.requests[3]["messages"][-1]["content"]


def test_too_many_requests_still_answers_and_reports(chat: Any) -> None:
    c, st, _ = chat
    st.reply = lambda body: tool_calls(("engine_logs", {"limit": len(body["messages"])}))
    body = ask(c, "loop forever").json()
    assert "couldn't finish" in body["message"] and body["report"]["class"] == "loop"
    assert len(st.requests) <= chat_harness.REQUEST_LIMIT


def test_unknown_action_type_is_reported(chat: Any) -> None:
    c, st, _ = chat
    st.replies += [reply("ok", [{"type": "teleport"}]), reply("ok", [{"type": "teleport"}])]
    body = ask(c, "teleport me").json()
    assert body["report"]["class"] == "unknown"
    assert "teleport" in incidents()[0]["message"]


def test_a_model_without_tools_gets_prompted_json(chat: Any) -> None:
    c, st, _ = chat
    st.replies += [
        (400, {"error": {"message": "This model does not support tools"}}),
        json.dumps({"message": "plain JSON answer", "choices": ["ok"], "actions": []}),
    ]
    body = ask(c, "hi").json()
    assert body["message"] == "plain JSON answer" and body["choices"] == ["ok"]
    assert "tools" not in st.requests[1]
    st.replies.append(json.dumps({"message": "again", "actions": []}))
    assert ask(c, "hi").json()["message"] == "again"  # remembered: no tools tried this time
    assert "tools" not in st.requests[2] and incidents() == []


@pytest.mark.parametrize("status", [401, 429])
def test_user_side_errors_are_not_reported(chat: Any, status: int) -> None:
    c, st, _ = chat
    st.replies.append((status, {"error": {"message": "nope"}}))
    r = ask(c, "hi")
    assert r.status_code == 502 and str(status) in r.json()["detail"]
    assert incidents() == []


def test_missing_key_is_the_users_to_fix(chat: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _, _ = chat
    monkeypatch.delenv("OPENROUTER_API_KEY")
    r = ask(c, "hi")
    assert r.status_code == 502 and "OPENROUTER_API_KEY is not set" in r.json()["detail"]
    assert incidents() == []


def test_lookup_errors_are_strings_not_crashes(chat: Any) -> None:
    c, st, _ = chat
    st.replies += [
        tool_calls(
            ("read_pipeline_file", {"path": "../secrets.env"}),
            ("run_details", {"run_id": "ghost"}),
            ("read_skill", {"name": "ghost"}),
        ),
        reply("nothing there"),
    ]
    body = ask(c, "look around").json()
    assert body["message"] == "nothing there" and body["report"] is None
    results = [m["content"] for m in st.requests[1]["messages"] if m["role"] == "tool"]
    assert all(x.startswith("error:") for x in results), results


def test_a_bug_in_a_tool_is_an_error_report(chat: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    c, st, _ = chat

    def boom(ctx: RunContext[Any], limit: int = 200) -> str:
        raise RuntimeError("bug in a tool")

    boom.__doc__ = chat_harness.engine_logs.__doc__
    monkeypatch.setattr(
        chat_harness,
        "TOOLS",
        tuple(boom if t is chat_harness.engine_logs else t for t in chat_harness.TOOLS),
    )
    boom.__name__ = "engine_logs"
    st.replies.append(tool_calls(("engine_logs", {"limit": 5})))
    r = ask(c, "logs?")
    assert r.status_code == 502
    (inc,) = incidents()
    assert inc["context"]["class"] == "error" and inc["exception_type"] == "RuntimeError"


def test_anthropic_wire_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same loop on the Anthropic Messages API: tool_use in, tool_result back."""
    import threading
    from http.server import BaseHTTPRequestHandler

    from .fakes import _Server

    seen: list[dict[str, Any]] = []
    answers = [
        {"type": "tool_use", "id": "tu_1", "name": "engine_logs", "input": {"limit": 3}},
        {"type": "tool_use", "id": "tu_2", "name": "reply",
         "input": {"message": "from claude", "choices": [], "actions": []}},
    ]  # fmt: skip

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            data = json.dumps({
                "id": f"msg_{len(seen)}", "type": "message", "role": "assistant",
                "model": body["model"], "content": [answers.pop(0)],
                "stop_reason": "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            }).encode()  # fmt: skip
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = _Server(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    try:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        base = f"http://127.0.0.1:{server.server_address[1]}"
        cfg = Config.from_dict(
            {
                "providers": {"anth": {"type": "anthropic", "base_url": base,
                                       "api_key_env": "ANTHROPIC_API_KEY"}},
                "models": {"son": {"provider": "anth", "model": "claude-sonnet-5-5",
                                   "input_usd_per_mtok": 3, "output_usd_per_mtok": 15}},
            }
        )  # fmt: skip
        (tmp_path / "p").mkdir()
        app = App(SqliteLedger(tmp_path / "l.db"))
        c = TestClient(
            create_server(
                app, [], pipelines_dir=tmp_path / "p", registry=default_registry(cfg), config=cfg
            )
        )
        body = ask(c, "hi", model="son").json()
    finally:
        server.shutdown()
        server.server_close()
    assert body["message"] == "from claude" and body["steps"] == ["engine log"]
    assert body["cost_usd"] == pytest.approx(2 * (1000 * 3 + 100 * 15) / 1e6)
    assert "SETTINGS" in str(seen[0]["system"]) and seen[0]["max_tokens"] == 16_000
    msgs = seen[1]["messages"]
    blocks = [b for m in msgs if isinstance(m["content"], list) for b in m["content"]]
    assert any(b.get("type") == "tool_result" and b["tool_use_id"] == "tu_1" for b in blocks)


def test_history_is_real_turns_and_images_come_as_transcripts() -> None:
    from pydantic_ai.messages import ModelRequest, ModelResponse

    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": '{"message": "hello"}'},
        {"role": "user", "content": "build this", "attachments": ["a.png"]},
    ]
    history, prompt = chat_harness._history(
        msgs, {"a.png": {"text": "STEPS:\n- plan", "by": "haiku-api", "edited": False}}
    )
    assert [type(m) for m in history] == [ModelRequest, ModelResponse]
    assert prompt.startswith("build this") and "STEPS:\n- plan" in prompt
    assert "by haiku-api" in prompt


def test_a_model_the_harness_cant_use_is_refused(chat: Any) -> None:
    c, st, _ = chat
    r = ask(c, "hi", model="opus")  # the Claude login: only claude-code can use it
    assert r.status_code == 422 and "can't run" in r.json()["detail"]
    assert st.requests == []
