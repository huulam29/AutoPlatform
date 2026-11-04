"""Slack and Discord links: the websocket client, event translation, outgoing messages, and the
chat-link loop over a non-Telegram transport."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest

from agentline import App, SqliteLedger
from agentline.chatlink import state
from agentline.chatlink.discord import Discord
from agentline.chatlink.service import ChatLink
from agentline.chatlink.slack import Slack
from agentline.chatlink.ws import WebSocket, WebSocketClosed

from .fake_ws import FakeWS
from .fakes import _Server


def test_websocket_client_frames_ping_and_continuation() -> None:
    srv = FakeWS([{"hello": "a" * 300}, {"n": 2}], ping_first=True)
    ws = WebSocket(srv.url)
    assert ws.recv_json() == {"hello": "a" * 300}  # split over two frames, after a ping
    assert ws.recv_json() == {"n": 2}
    ws.send_json({"ack": 1})
    assert srv.got_pong.wait(2)
    ws.close()
    time.sleep(0.1)
    assert srv.received == [{"ack": 1}]
    with pytest.raises(ValueError):
        WebSocket("http://example.com")


@contextmanager
def fake_api(responses: dict[str, Any]) -> Iterator[tuple[str, list[tuple[str, str, Any]]]]:
    calls: list[tuple[str, str, Any]] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _do(self) -> None:
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            calls.append((self.command, self.path, body))
            r = responses.get(f"{self.command} {self.path}", responses.get(self.path, {"ok": True}))
            raw = json.dumps(r).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PATCH = _do

    server = _Server(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", calls
    finally:
        server.shutdown()
        server.server_close()


def test_slack_translates_events_acks_and_sends_blocks() -> None:
    srv = FakeWS(
        [
            {"type": "hello"},
            {
                "envelope_id": "e1",
                "type": "events_api",
                "payload": {
                    "event": {
                        "type": "message",
                        "channel_type": "im",
                        "channel": "D1",
                        "user": "U1",
                        "text": "/status",
                        "ts": "100.1",
                    }
                },
            },
            {
                "envelope_id": "e2",
                "type": "events_api",
                "payload": {
                    "event": {
                        "type": "message",
                        "channel": "D1",
                        "user": "U1",
                        "bot_id": "B",
                        "text": "mine",
                        "ts": "100.2",
                    }
                },
            },
            {
                "envelope_id": "e3",
                "type": "events_api",
                "payload": {
                    "event": {
                        "type": "message",
                        "channel_type": "im",
                        "channel": "D1",
                        "user": "U1",
                        "text": "staging",
                        "ts": "100.4",
                        "thread_ts": "100.3",
                    }
                },
            },
            {
                "envelope_id": "e4",
                "type": "interactive",
                "payload": {
                    "type": "block_actions",
                    "user": {"id": "U1"},
                    "channel": {"id": "D1"},
                    "container": {"message_ts": "100.3"},
                    "actions": [{"value": "ga:1", "action_ts": "9"}],
                },
            },
        ]
    )
    with fake_api(
        {
            "/apps.connections.open": {"ok": True, "url": srv.url},
            "/chat.postMessage": {"ok": True, "ts": "200.1"},
            "/auth.test": {"ok": True, "user": "agentline", "user_id": "UB"},
        }
    ) as (base, calls):
        s = Slack("xapp-1", "xoxb-1", base=base)
        got: list[dict[str, Any]] = []
        end = time.monotonic() + 3
        while len(got) < 3 and time.monotonic() < end:
            got += s.updates(None, 1)
        assert [g.get("message", {}).get("text") for g in got[:2]] == ["/status", "staging"]
        assert got[0]["message"]["chat"] == {"id": "D1", "type": "private"}
        assert got[1]["message"]["reply_to_message"] == {"message_id": "100.3"}
        assert got[2]["callback_query"]["data"] == "ga:1"
        assert got[2]["callback_query"]["message"]["message_id"] == "100.3"
        mid = s.send(
            "D1", "Approve?", buttons=[[("Approve", "ga:1"), ("Reject", "gr:1")]], reply_to="1.0"
        )
        assert mid == "200.1" and s.me()["username"] == "agentline"
        s.close()
        post = next(b for m, p, b in calls if p == "/chat.postMessage")
        assert post["thread_ts"] == "1.0" and post["blocks"][1]["elements"][1]["value"] == "gr:1"
    time.sleep(0.1)
    assert {r["envelope_id"] for r in srv.received} == {"e1", "e2", "e3", "e4"}  # all acked


def test_discord_identifies_translates_and_sends_components() -> None:
    srv = FakeWS(
        [
            {"op": 10, "d": {"heartbeat_interval": 60000}},
            {"op": 0, "s": 1, "t": "READY", "d": {"user": {"username": "agentline", "id": "B"}}},
            {
                "op": 0,
                "s": 2,
                "t": "MESSAGE_CREATE",
                "d": {
                    "id": "11",
                    "channel_id": "C1",
                    "content": "123456",
                    "author": {"id": "U1", "username": "me"},
                },
            },
            {
                "op": 0,
                "s": 3,
                "t": "MESSAGE_CREATE",
                "d": {
                    "id": "12",
                    "channel_id": "C1",
                    "content": "bot says",
                    "author": {"id": "B", "bot": True},
                },
            },
            {
                "op": 0,
                "s": 4,
                "t": "INTERACTION_CREATE",
                "d": {
                    "id": "I1",
                    "token": "tok",
                    "type": 3,
                    "channel_id": "C1",
                    "user": {"id": "U1"},
                    "data": {"custom_id": "rt:2"},
                    "message": {"id": "13"},
                },
            },
        ]
    )
    with fake_api(
        {
            "/gateway/bot": {"url": srv.url.split("/socket")[0]},
            "/channels/C1/messages": {"id": "14"},
        }
    ) as (base, calls):
        d = Discord("tok", base=base)
        got: list[dict[str, Any]] = []
        end = time.monotonic() + 3
        while len(got) < 2 and time.monotonic() < end:
            got += d.updates(None, 1)
        assert (
            got[0]["message"]["text"] == "123456" and got[0]["message"]["chat"]["type"] == "private"
        )
        tap = got[1]["callback_query"]
        assert tap["data"] == "rt:2" and tap["id"] == "I1:tok"
        assert d.me()["username"] == "agentline"
        d.answer(tap["id"])
        assert d.send("C1", "hi", buttons=[[("Retry", "rt:2")]], reply_to="11") == "14"
        d.clear_buttons("C1", "13")
        d.close()
        methods = {(m, p) for m, p, _ in calls}
        assert ("POST", "/interactions/I1/tok/callback") in methods
        assert ("PATCH", "/channels/C1/messages/13") in methods
        msg = next(b for m, p, b in calls if p == "/channels/C1/messages")
        assert msg["components"][0]["components"][0]["custom_id"] == "rt:2"
        assert msg["message_reference"] == {"message_id": "11"}
    time.sleep(0.1)
    assert srv.received[0]["op"] == 2 and srv.received[0]["d"]["intents"] == (1 << 12) | (1 << 15)


class FakeTransport:
    """Any platform as the loop sees it."""

    def __init__(self) -> None:
        self.inbox: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.n = 0

    def updates(self, offset: Any, wait: int = 0) -> list[dict[str, Any]]:
        out, self.inbox = self.inbox, []
        return out

    def send(self, chat: Any, text: str, **kw: Any) -> str:
        self.n += 1
        self.sent.append({"chat": chat, "text": text, **kw})
        return f"m{self.n}"

    def answer(self, *a: Any, **kw: Any) -> None:
        pass

    def clear_buttons(self, *a: Any) -> None:
        pass

    def say(self, text: str, user: str = "U1", reply_to: str | None = None) -> None:
        m: dict[str, Any] = {
            "message_id": f"in{len(self.inbox)}",
            "chat": {"id": "D1", "type": "private"},
            "from": {"id": user, "username": "me"},
            "text": text,
        }
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        self.inbox.append({"update_id": 1, "message": m})


def test_the_loop_works_over_slack_ids(tmp_path: Path) -> None:
    app = App(SqliteLedger(tmp_path / "l.db"))
    t = FakeTransport()
    link = ChatLink(app, poll_wait=0, platform="slack", transport=lambda: t)
    code = state.ChatState("slack").new_pair_code()
    t.say(code)
    asyncio.run(link.step(t))
    assert state.ChatState("slack").linked()["chat_id"] == "D1"  # string ids stay strings
    assert state.ChatState("telegram").linked() is None  # separate from Telegram
    t.say("/runs", user="U2")
    asyncio.run(link.step(t))
    assert "only answers its owner" in t.sent[-1]["text"]
    t.say("/runs")
    asyncio.run(link.step(t))
    assert t.sent[-1]["text"] == "No runs yet."


def test_chat_api_lists_every_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from agentline.api.server import create_server

    for k in ("TELEGRAM_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "DISCORD_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "p").mkdir()
    c = TestClient(
        create_server(App(SqliteLedger(tmp_path / "a.db")), [], pipelines_dir=tmp_path / "p")
    )
    v = c.get("/api/chat").json()
    assert v["platforms"] == ["telegram", "slack", "discord"]
    assert [k["name"] for k in v["slack"]["keys"]] == ["SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"]
    assert not v["discord"]["token_set"] and "MESSAGE CONTENT" in v["discord"]["setup"]
    assert c.post("/api/chat/slack/pair").status_code == 409
    assert c.post("/api/chat/nope/pair").status_code == 404
    keys = {k["name"] for k in c.get("/api/settings").json()["keys"]}
    assert {"SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "DISCORD_BOT_TOKEN"} <= keys
    with pytest.raises(WebSocketClosed):
        raise WebSocketClosed("x")


@pytest.mark.parametrize("platform", ["slack", "discord"])
def test_slack_and_discord_approvals_name_the_person(
    platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 011: the link acts as the person who asked for its code, else the chat user."""
    from agentline.builtins import default_registry
    from agentline.people import PeopleStore
    from agentline.pipeline import build_pipeline, parse_spec

    monkeypatch.setenv("AGENTLINE_USER", "osuser")
    app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0.0)
    src = 'version: 1\nname: g\nnodes:\n  ok: { type: gate, prompt: "Go?" }\n'
    app.register_pipeline(build_pipeline(parse_spec(src, base_dir=tmp_path), default_registry()))
    PeopleStore().add("alice")
    t = FakeTransport()
    link = ChatLink(app, poll_wait=0, platform=platform, transport=lambda: t)
    t.say(state.ChatState(platform).new_pair_code("alice"))
    asyncio.run(link.step(t))

    def approve() -> list[str]:
        run_id = app.submit_sync("g", {}).run_id
        asyncio.run(app.worker().run_until_idle())
        t.say(f"/approve {run_id}")
        asyncio.run(link.step(t))
        with app.ledger.tx() as tx:
            return [(a.actor, a.detail.get("via")) for a in tx.list_audit(run_id)
                    if a.action == "approve"]  # fmt: skip

    assert approve() == [("alice", f"{platform}:@me")]
    PeopleStore().revoke("alice")
    assert approve() == [(f"{platform}:@me", f"{platform}:@me")]
