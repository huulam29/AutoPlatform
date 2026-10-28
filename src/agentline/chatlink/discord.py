"""Discord link (Gateway websocket: no public URL). Key DISCORD_BOT_TOKEN on its card
(Settings > Chat apps).

In the Discord developer portal: create an application, add a bot, enable the MESSAGE CONTENT
intent, and invite the bot to a server you're in (so you can DM it). Then DM the bot the
pairing code. Reply to a question's message to answer it; buttons are message components.
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Any

from agentline.chatlink.sockets import (
    LinkError,
    SocketTransport,
    http_json,
    message_update,
    tap_update,
)
from agentline.chatlink.ws import WebSocket

TOKEN = "DISCORD_BOT_TOKEN"
API = "https://discord.com/api/v10"
INTENTS = (1 << 12) | (1 << 15)  # DIRECT_MESSAGES | MESSAGE_CONTENT


def api_base() -> str:
    return os.environ.get("AGENTLINE_DISCORD_API") or API


class Discord(SocketTransport):
    def __init__(self, token: str, base: str | None = None, listen: bool = True) -> None:
        self.token = token
        self.base = (base or api_base()).rstrip("/")
        self._me: dict[str, Any] | None = None
        self._seq: Any = None
        self._beat: threading.Thread | None = None
        super().__init__(listen)

    def _http(self, method: str, path: str, body: Any = None) -> Any:
        return http_json(method, f"{self.base}{path}", {"authorization": f"Bot {self.token}"}, body)

    def connect(self) -> WebSocket:
        url = self._http("GET", "/gateway/bot")["url"]
        ws = WebSocket(f"{url}?v=10&encoding=json")
        hello = ws.recv_json()
        interval = float((hello.get("d") or {}).get("heartbeat_interval") or 41250) / 1000
        ws.send_json(
            {
                "op": 2,
                "d": {
                    "token": self.token,
                    "intents": INTENTS,
                    "properties": {"os": "macos", "browser": "agentline", "device": "agentline"},
                },
            }
        )

        def beat() -> None:
            while not self._stop.wait(interval) and self._ws is ws:
                try:
                    ws.send_json({"op": 1, "d": self._seq})
                except OSError:
                    return

        self._beat = threading.Thread(target=beat, daemon=True)
        self._beat.start()
        return ws

    def handle(self, ws: WebSocket, m: dict[str, Any]) -> None:
        if m.get("s") is not None:
            self._seq = m["s"]
        op = m.get("op")
        if op in (7, 9):  # reconnect / invalid session
            raise LinkError("Discord asked to reconnect")
        if op == 1:
            ws.send_json({"op": 1, "d": self._seq})
            return
        if op != 0:
            return
        t, d = m.get("t"), m.get("d") or {}
        if t == "READY":
            u = d.get("user") or {}
            self._me = {"username": u.get("username"), "id": u.get("id")}
        elif t == "MESSAGE_CREATE":
            a = d.get("author") or {}
            if a.get("bot"):
                return
            ref = (d.get("message_reference") or {}).get("message_id")
            self.push(
                message_update(
                    str(d.get("id")),
                    str(d.get("channel_id")),
                    str(a.get("id")),
                    str(a.get("username") or a.get("id")),
                    str(d.get("content") or ""),
                    d.get("guild_id") is None,
                    str(ref) if ref else None,
                )
            )
        elif t == "INTERACTION_CREATE" and d.get("type") == 3:
            user = d.get("user") or (d.get("member") or {}).get("user") or {}
            self.push(
                tap_update(
                    f"{d.get('id')}:{d.get('token')}",
                    str(d.get("channel_id")),
                    str(user.get("id")),
                    str((d.get("data") or {}).get("custom_id") or ""),
                    str((d.get("message") or {}).get("id")),
                )
            )

    # -- out ---------------------------------------------------------------------------------
    def me(self) -> dict[str, Any]:
        if self._me is None:
            u = self._http("GET", "/users/@me")
            self._me = {"username": u.get("username"), "id": u.get("id")}
        return self._me

    def send(
        self,
        chat: Any,
        text: str,
        *,
        buttons: list[list[tuple[str, str]]] | None = None,
        reply_to: Any = None,
        force_reply: bool = False,
    ) -> str:
        body: dict[str, Any] = {"content": text[:1990]}
        if buttons:
            body["components"] = [
                {
                    "type": 1,
                    "components": [
                        {"type": 2, "style": 1, "label": label[:80], "custom_id": data[:100]}
                        for label, data in row[:5]
                    ],
                }
                for row in buttons[:5]
            ]
        if reply_to:
            body["message_reference"] = {"message_id": str(reply_to)}
        return str(self._http("POST", f"/channels/{chat}/messages", body)["id"])

    def answer(self, callback_id: str, text: str = "") -> None:
        iid, _, token = callback_id.partition(":")
        if iid and token:  # acknowledge the button press (must happen within 3 s)
            self._http("POST", f"/interactions/{iid}/{token}/callback", {"type": 6})

    def clear_buttons(self, chat: Any, message_id: Any) -> None:
        with contextlib.suppress(LinkError):
            self._http("PATCH", f"/channels/{chat}/messages/{message_id}", {"components": []})


def from_env(listen: bool = True) -> Discord | None:
    t = os.environ.get(TOKEN)
    return Discord(t, listen=listen) if t else None
