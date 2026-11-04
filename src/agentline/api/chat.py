"""Chat links API (agentline.chatlink): Telegram, Slack, Discord.

GET    /chat                  {telegram, slack, discord, notify, platforms}; each platform:
                              {title, keys: [{name, set}], token_env, token_set, bot, linked,
                              acts_as, pairing, setup}; `acts_as` is who the link's actions
                              are audited as (a person or `<platform>:@<user>`, spec 011)
POST   /chat/{platform}/pair         a 6-digit code (10 minutes) to send the bot (+ t.me link);
                                     asked with a person's token, the link acts as them
POST   /chat/{platform}/test         send a test message to the linked chat
DELETE /chat/{platform}              unlink
PUT    /chat/notify                  {gates?, failures?, done?, questions?, reviews?} (shared)
GET    /chat-instructions            {text, default, is_default, path}: what every Chat turn is
                                     told (agentline.chat_instructions)
PUT    /chat-instructions {text}     replace them
DELETE /chat-instructions            back to the defaults
POST   /chat-instructions/lesson {text}   append one lesson line (failure triage)
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agentline import chat_instructions
from agentline.api.identity import identity_of
from agentline.chatlink import state
from agentline.chatlink.platforms import PLATFORMS, whoami
from agentline.chatlink.service import link_actor
from agentline.errors import ValidationError
from agentline.people import PeopleStore


class InstructionsBody(BaseModel):
    text: str


def instructions_view() -> dict[str, Any]:
    text = chat_instructions.read()
    return {
        "text": text,
        "default": chat_instructions.DEFAULT,
        "is_default": text == chat_instructions.DEFAULT,
        "path": str(chat_instructions.path()),
    }


class NotifyBody(BaseModel):
    gates: bool | None = None
    failures: bool | None = None
    done: bool | None = None
    questions: bool | None = None
    reviews: bool | None = None


def create_chat_router() -> APIRouter:
    router = APIRouter()
    bots: dict[str, tuple[str, str | None]] = {}  # platform -> (keys fingerprint, bot name)
    people = PeopleStore()

    def platform(name: str) -> Any:
        p = PLATFORMS.get(name)
        if p is None:
            raise HTTPException(404, f"unknown chat platform {name!r}")
        return p

    async def bot_name(name: str) -> str | None:
        p = PLATFORMS[name]
        if not p.keys_set():
            return None
        fp = "|".join(os.environ.get(k, "")[-6:] for k in p.keys)
        cached = bots.get(name)
        if cached and cached[0] == fp and cached[1]:
            return cached[1]
        bot = await asyncio.to_thread(whoami, name)
        bots[name] = (fp, bot)
        return bot

    async def one(name: str) -> dict[str, Any]:
        p = PLATFORMS[name]
        st = state.ChatState(name)
        d = st.read()
        pair = d.get("pair") or {}
        pending = pair if pair and float(pair.get("expires") or 0) > time.time() else None
        linked = st.linked()
        return {
            "title": p.title,
            "keys": [{"name": k, "set": bool(os.environ.get(k))} for k in p.keys],
            "token_env": p.keys[0],
            "token_set": p.keys_set(),
            "bot": await bot_name(name),
            "linked": linked,
            "acts_as": link_actor(name, linked, people) if linked else None,
            "pairing": pending,
            "setup": p.setup,
        }

    async def view() -> dict[str, Any]:
        out: dict[str, Any] = {name: await one(name) for name in PLATFORMS}
        out["notify"] = state.notify_prefs()
        out["platforms"] = list(PLATFORMS)
        return out

    @router.get("/chat-instructions")
    def get_instructions() -> dict[str, Any]:
        return instructions_view()

    @router.put("/chat-instructions")
    def put_instructions(body: InstructionsBody) -> dict[str, Any]:
        try:
            chat_instructions.write(body.text)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return instructions_view()

    @router.post("/chat-instructions/lesson")
    def add_lesson(body: InstructionsBody) -> dict[str, Any]:
        try:
            chat_instructions.add_lesson(body.text)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return instructions_view()

    @router.delete("/chat-instructions")
    def reset_instructions() -> dict[str, Any]:
        chat_instructions.reset()
        return instructions_view()

    @router.get("/chat")
    async def get_chat() -> dict[str, Any]:
        return await view()

    @router.post("/chat/{name}/pair")
    async def pair(name: str, request: Request) -> dict[str, Any]:
        p = platform(name)
        if not p.keys_set():
            msg = f"set {', '.join(p.keys)} on its card under Settings > Chat apps first"
            raise HTTPException(409, msg)
        bot = await bot_name(name)
        if bot is None:
            raise HTTPException(502, f"{p.title} didn't accept the bot token (check it)")
        who = identity_of(request)  # a person's code links a chat that acts as them (spec 011)
        code = state.ChatState(name).new_pair_code(who.name if who.kind == "person" else None)
        link = f"https://t.me/{bot}?start={code}" if name == "telegram" else None
        return {**(await view()), "platform": name, "code": code, "link": link}

    @router.post("/chat/{name}/test")
    async def test(name: str) -> dict[str, Any]:
        p = platform(name)
        link = state.ChatState(name).linked()
        t = p.connect(False)
        if t is None or not link:
            raise HTTPException(409, "link a chat first")
        try:
            await asyncio.to_thread(t.send, link["chat_id"], "Test from agentline: the link works.")
        except Exception as e:  # TelegramError, LinkError
            raise HTTPException(502, str(e)) from None
        return {"sent": True}

    @router.delete("/chat/{name}")
    async def unlink(name: str) -> dict[str, Any]:
        platform(name)
        with state.ChatState(name).edit() as d:
            for k in (name, "pair", "history", "proposals", "asks", "ask_choices"):
                d.pop(k, None)
        return await view()

    @router.put("/chat/notify")
    async def notify(body: NotifyBody) -> dict[str, Any]:
        with state.edit() as d:
            prefs = state.notify_prefs(d)
            prefs.update({k: v for k, v in body.model_dump().items() if v is not None})
            d["notify"] = prefs
        return await view()

    return router
