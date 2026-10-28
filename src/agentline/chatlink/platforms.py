"""The chat platforms agentline can link to, their keys and how to set them up."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentline.chatlink import discord, slack
from agentline.chatlink.telegram import TOKEN_ENV as TELEGRAM_TOKEN
from agentline.chatlink.telegram import Telegram


@dataclass(frozen=True)
class Platform:
    name: str
    title: str
    keys: tuple[str, ...]
    setup: str
    connect: Callable[[bool], Any]  # (listen) -> transport, or None while keys are missing

    def keys_set(self) -> bool:
        return all(os.environ.get(k) for k in self.keys)


def _telegram(listen: bool = True) -> Any:
    t = os.environ.get(TELEGRAM_TOKEN)
    return Telegram(t) if t else None


PLATFORMS: dict[str, Platform] = {
    "telegram": Platform(
        "telegram",
        "Telegram",
        (TELEGRAM_TOKEN,),
        "In Telegram, open @BotFather, send /newbot and follow the steps; paste the token.",
        _telegram,
    ),
    "slack": Platform(
        "slack",
        "Slack",
        (slack.APP_TOKEN, slack.BOT_TOKEN),
        "Create a Slack app (api.slack.com/apps), turn on Socket Mode, Interactivity and the"
        " message.im bot event, add the bot scopes chat:write, im:history, im:read, users:read,"
        " install it, then paste the app-level token (xapp-, connections:write) and the bot"
        " token (xoxb-).",
        slack.from_env,
    ),
    "discord": Platform(
        "discord",
        "Discord",
        (discord.TOKEN,),
        "Create an application at discord.com/developers, add a bot, turn on the MESSAGE CONTENT"
        " intent, invite the bot to a server you're in (so you can DM it), and paste its token.",
        discord.from_env,
    ),
}


def whoami(name: str) -> str | None:
    """The bot's username (over HTTP; no connection kept), or None when keys are missing or
    refused."""
    p = PLATFORMS[name]
    if not p.keys_set():
        return None
    try:
        t = p.connect(False)
        return None if t is None else (t.me() or {}).get("username")
    except Exception:  # bad token, offline
        return None
