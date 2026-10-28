"""A fake Telegram Bot API: queue updates, record what the bot sends."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any

from .fakes import _Server

TOKEN = "123456:test-bot-token"
OWNER = 42
STRANGER = 99


@dataclass
class FakeTelegram:
    updates: list[dict[str, Any]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)
    next_update: int = 1
    next_message: int = 100

    def text(self, text: str, user: int = OWNER, reply_to: int | None = None) -> None:
        msg: dict[str, Any] = {
            "message_id": self.next_message,
            "chat": {"id": user, "type": "private"},
            "from": {"id": user, "username": f"user{user}"},
            "text": text,
        }
        self.next_message += 1
        if reply_to is not None:
            msg["reply_to_message"] = {"message_id": reply_to}
        self._push({"message": msg})

    def tap(self, data: str, user: int = OWNER, message_id: int = 1) -> None:
        self._push({"callback_query": {
            "id": f"cb{self.next_update}", "from": {"id": user}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": user}},
        }})  # fmt: skip

    def _push(self, u: dict[str, Any]) -> None:
        self.updates.append({"update_id": self.next_update, **u})
        self.next_update += 1

    def last(self) -> dict[str, Any]:
        return self.sent[-1]

    def buttons(self, msg: dict[str, Any]) -> list[tuple[str, str]]:
        rows = ((msg.get("reply_markup") or {}).get("inline_keyboard")) or []
        return [(b["text"], b["callback_data"]) for row in rows for b in row]


@contextmanager
def fake_telegram() -> Iterator[tuple[str, FakeTelegram]]:
    st = FakeTelegram()

    class H(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(
                self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}"
            )
            _, bot, method = self.path.split("/", 2)
            if bot != f"bot{TOKEN}":
                return self.reply(401, {"ok": False, "description": "Unauthorized"})
            if method == "getMe":
                return self.reply(200, {"ok": True, "result": {"username": "agentline_test_bot"}})
            if method == "getUpdates":
                off = body.get("offset") or 0
                due = [u for u in st.updates if u["update_id"] >= off]
                return self.reply(200, {"ok": True, "result": due})
            if method == "sendMessage":
                mid = st.next_message
                st.next_message += 1
                st.sent.append({**body, "message_id": mid})
                return self.reply(200, {"ok": True, "result": {"message_id": mid}})
            if method == "answerCallbackQuery":
                st.answered.append(body["callback_query_id"])
            return self.reply(200, {"ok": True, "result": True})

        def reply(self, status: int, data: Any) -> None:
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = _Server(("127.0.0.1", 0), H)
    t = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", st
    finally:
        server.shutdown()
        server.server_close()
