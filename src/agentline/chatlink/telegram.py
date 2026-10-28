"""Telegram Bot API client: getUpdates (long polling), sendMessage, callbacks.

The token is part of every URL, so errors never include the URL (only the method and status).
`AGENTLINE_TELEGRAM_API` points at a fake server in tests.
"""

from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.request
from typing import Any

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
MAX_TEXT = 4000  # Telegram's limit is 4096


class TelegramError(Exception):
    pass


def api_base() -> str:
    return os.environ.get("AGENTLINE_TELEGRAM_API") or "https://api.telegram.org"


def token() -> str | None:
    return os.environ.get(TOKEN_ENV) or None


class Telegram:
    def __init__(self, bot_token: str, base: str | None = None) -> None:
        self._token = bot_token
        self.base = (base or api_base()).rstrip("/")

    def call(self, method: str, http_timeout: float = 30, **params: Any) -> Any:
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(
            f"{self.base}/bot{self._token}/{method}",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                desc = json.loads(e.read()).get("description", "")
            except ValueError:
                desc = ""
            raise TelegramError(f"Telegram {method}: HTTP {e.code} {desc}".strip()) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise TelegramError(
                f"Telegram {method}: can't connect ({type(reason).__name__})"
            ) from None
        if not data.get("ok"):
            raise TelegramError(f"Telegram {method}: {data.get('description', 'failed')}")
        return data.get("result")

    def me(self) -> dict[str, Any]:
        return dict(self.call("getMe", http_timeout=15))

    def updates(self, offset: int | None, wait: int = 20) -> list[dict[str, Any]]:
        return list(
            self.call(
                "getUpdates",
                http_timeout=wait + 10,
                offset=offset,
                timeout=wait,
                allowed_updates=["message", "callback_query"],
            )
            or []
        )

    def send(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: list[list[tuple[str, str]]] | None = None,
        reply_to: int | None = None,
        force_reply: bool = False,
    ) -> int:
        """Send text (cut to Telegram's limit); `buttons` rows of (label, callback data)."""
        markup: dict[str, Any] | None = None
        if buttons:
            markup = {
                "inline_keyboard": [
                    [{"text": label[:60], "callback_data": data[:64]} for label, data in row]
                    for row in buttons
                ]
            }
        elif force_reply:
            markup = {"force_reply": True}
        msg = self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:MAX_TEXT] or "(empty)",
            reply_markup=markup,
            reply_parameters={"message_id": reply_to} if reply_to else None,
            link_preview_options={"is_disabled": True},
        )
        return int(msg["message_id"])

    def answer(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190] or None)

    def clear_buttons(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(TelegramError):  # too old to edit: not worth failing over
            self.call(
                "editMessageReplyMarkup",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup={"inline_keyboard": []},
            )
