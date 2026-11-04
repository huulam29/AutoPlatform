"""The chat link's local state: `$AGENTLINE_HOME/chatlink.json` (0600).

{"telegram": {"chat_id", "user_id", "username", "linked_at", "person"?} | null,
 "pair": {"code", "expires", "person"?}, a pending pairing code (10 minutes); `person` is
                                         who asked for it (spec 011)
 "offset": <next update id>,
 "notify": {"gates", "failures", "done", "questions", "reviews"},
 "sent": {"gate:<exec id>": message_id, "run:<id>:<status>": message_id, ...},
 "asks": {"<message id>": "<correlation key>"},
 "history": [{role, content}], "proposals": [...]}    the assistant conversation
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

DEFAULT_NOTIFY = {
    "gates": True,
    "failures": True,
    "done": False,
    "questions": True,
    "reviews": False,  # run reviews (agentline.review)
}
PAIR_SECONDS = 600


def home() -> Path:
    return Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")


def state_path(platform: str = "telegram") -> Path:
    """Telegram keeps `chatlink.json` (where the shared notify settings also live); every
    other platform has `chatlink-<platform>.json`."""
    return home() / ("chatlink.json" if platform == "telegram" else f"chatlink-{platform}.json")


LIMITS = {"sent": 1000, "gates": 300, "ask_choices": 300, "asks": 300, "retries": 300,
          "cards": 300, "thread_msgs": 1000, "threads": 300}  # fmt: skip


def prune(data: dict[str, Any]) -> dict[str, Any]:
    """Keep the bookkeeping maps bounded: the newest entries (insertion order) win."""
    for key, cap in LIMITS.items():
        m = data.get(key)
        if isinstance(m, dict) and len(m) > cap:
            data[key] = dict(list(m.items())[-cap:])
    if isinstance(data.get("history"), list):
        data["history"] = data["history"][-40:]
    return data


class ChatState:
    """One platform's link (data[platform]), pairing code, sent messages, open questions and
    assistant conversation."""

    def __init__(self, platform: str = "telegram") -> None:
        self.platform = platform

    @property
    def path(self) -> Path:
        return state_path(self.platform)

    def read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        prune(data)
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    @contextlib.contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        """Read-modify-write under a lock (the API and the loop both write)."""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(f".{path.stem}.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = self.read()
                yield data
                self._write(data)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def notify_prefs(self, data: dict[str, Any] | None = None) -> dict[str, bool]:
        """Shared by every platform (kept in chatlink.json)."""
        if data is None or self.platform != "telegram":
            data = ChatState("telegram").read() if self.platform != "telegram" else self.read()
        return {**DEFAULT_NOTIFY, **{k: bool(v) for k, v in (data.get("notify") or {}).items()}}

    def new_pair_code(self, person: str | None = None) -> str:
        """A fresh pairing code. `person`: the named person (spec 007) who asked for it; the link
        it pairs then acts as them in audit rows (spec 011)."""
        code = f"{secrets.randbelow(10**6):06d}"
        pair: dict[str, Any] = {"code": code, "expires": time.time() + PAIR_SECONDS}
        if person:
            pair["person"] = person
        with self.edit() as d:
            d["pair"] = pair
        return code

    def linked(self) -> dict[str, Any] | None:
        t = self.read().get(self.platform)
        return t if isinstance(t, dict) and t.get("chat_id") else None


# Telegram's state, as module functions (what the first version of the API and steps use)
_TELEGRAM = ChatState("telegram")
read = _TELEGRAM.read
edit = _TELEGRAM.edit
notify_prefs = _TELEGRAM.notify_prefs
new_pair_code = _TELEGRAM.new_pair_code
linked = _TELEGRAM.linked
