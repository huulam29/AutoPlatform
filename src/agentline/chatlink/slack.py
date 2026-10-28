"""Slack link (Socket Mode: no public URL). Two tokens on its card (Settings > Chat apps):

- SLACK_APP_TOKEN (xapp-..., an app-level token with `connections:write`)
- SLACK_BOT_TOKEN (xoxb-..., scopes chat:write, im:history, im:read, users:read)

Enable Socket Mode, Interactivity, and the `message.im` bot event in the app's settings, then
DM the bot the pairing code. Replies in a question's thread answer it; buttons are Block Kit.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from agentline.chatlink.sockets import (
    LinkError,
    SocketTransport,
    http_json,
    message_update,
    tap_update,
)
from agentline.chatlink.ws import WebSocket

APP_TOKEN = "SLACK_APP_TOKEN"
BOT_TOKEN = "SLACK_BOT_TOKEN"
API = "https://slack.com/api"


def api_base() -> str:
    return os.environ.get("AGENTLINE_SLACK_API") or API


class Slack(SocketTransport):
    def __init__(
        self, app_token: str, bot_token: str, base: str | None = None, listen: bool = True
    ) -> None:
        self.app_token = app_token
        self.bot_token = bot_token
        self.base = (base or api_base()).rstrip("/")
        self._me: dict[str, Any] | None = None
        super().__init__(listen)

    def _call(self, method: str, body: dict[str, Any], token: str | None = None) -> dict[str, Any]:
        r = http_json(
            "POST",
            f"{self.base}/{method}",
            {"authorization": f"Bearer {token or self.bot_token}"},
            body,
        )
        if not isinstance(r, dict) or not r.get("ok"):
            raise LinkError(f"Slack {method}: {(r or {}).get('error', 'failed')}")
        return r

    def connect(self) -> WebSocket:
        url = self._call("apps.connections.open", {}, self.app_token)["url"]
        return WebSocket(url)

    def handle(self, ws: WebSocket, m: dict[str, Any]) -> None:
        env_id = m.get("envelope_id")
        if env_id:
            ws.send_json({"envelope_id": env_id})  # ack within 3 s
        kind = m.get("type")
        if kind == "disconnect":
            raise LinkError("Slack asked to reconnect")
        payload = m.get("payload") or {}
        if kind == "events_api":
            e = payload.get("event") or {}
            if e.get("type") != "message" or e.get("bot_id") or e.get("subtype"):
                return
            thread = e.get("thread_ts")
            self.push(
                message_update(
                    str(e.get("ts")),
                    str(e.get("channel")),
                    str(e.get("user")),
                    str(e.get("user")),
                    str(e.get("text") or ""),
                    e.get("channel_type") == "im",
                    str(thread) if thread and thread != e.get("ts") else None,
                )
            )
        elif kind == "interactive" and payload.get("type") == "block_actions":
            action = (payload.get("actions") or [{}])[0]
            self.push(
                tap_update(
                    str(action.get("action_ts") or env_id),
                    str((payload.get("channel") or {}).get("id")),
                    str((payload.get("user") or {}).get("id")),
                    str(action.get("value") or ""),
                    str((payload.get("container") or {}).get("message_ts") or ""),
                )
            )

    # -- out ---------------------------------------------------------------------------------
    def me(self) -> dict[str, Any]:
        if self._me is None:
            r = self._call("auth.test", {})
            self._me = {"username": r.get("user"), "id": r.get("user_id")}
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
        body: dict[str, Any] = {"channel": chat, "text": text[:3900]}
        if buttons:
            body["blocks"] = [
                {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
                *(
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": label[:75]},
                                "value": data,
                                "action_id": f"a{i}.{j}",
                            }
                            for j, (label, data) in enumerate(row)
                        ],
                    }
                    for i, row in enumerate(buttons)
                ),
            ]
        if reply_to:
            body["thread_ts"] = reply_to
        return str(self._call("chat.postMessage", body)["ts"])

    def answer(self, callback_id: str, text: str = "") -> None:
        return  # the envelope ack is the answer in Socket Mode

    def clear_buttons(self, chat: Any, message_id: Any) -> None:
        with contextlib.suppress(LinkError):
            self._call("chat.update", {"channel": chat, "ts": message_id, "blocks": []})


def from_env(listen: bool = True) -> Slack | None:
    app, bot = os.environ.get(APP_TOKEN), os.environ.get(BOT_TOKEN)
    return Slack(app, bot, listen=listen) if app and bot else None
