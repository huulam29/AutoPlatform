"""Chat conversations, kept on the server so they survive a reload and the Mac app restarting.

One JSON file per conversation in `$AGENTLINE_HOME/assistant/conversations/<id>.json` (next to
the Chat's attachments): `{id, title, created_at, updated_at, kept_at, archived_at, messages}`.
The web Chat panel writes the whole message list (replies, card states) after every change;
the id is the client's, so the first save creates the file.

Archive rule: a conversation with no activity for ARCHIVE_DAYS is archived automatically
(hidden from the main History list, still searchable and openable). Activity is the last
message (`updated_at`) or an Unarchive (`kept_at`). Archive by hand sets `archived_at`; a new
message after that brings the conversation back. Nothing is ever deleted.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError

ARCHIVE_DAYS = 7
DAY = 86_400.0
MAX_TITLE = 80
MAX_BYTES = 8_000_000
_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def conversations_dir() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "assistant" / "conversations"


def valid_id(conv_id: str) -> bool:
    return bool(_ID.match(conv_id))


def is_archived(c: dict[str, Any], now: float | None = None) -> bool:
    """Archived by hand (and quiet since), or no activity for ARCHIVE_DAYS."""
    now = time.time() if now is None else now
    active = max(float(c.get("updated_at") or 0), float(c.get("kept_at") or 0))
    archived_at = c.get("archived_at")
    if archived_at is not None and float(archived_at) >= active:
        return True
    return now - active > ARCHIVE_DAYS * DAY


def title_of(messages: list[dict[str, Any]]) -> str:
    """The first user message's first line, shortened."""
    for m in messages:
        if m.get("role") == "user":
            text = " ".join(str(m.get("content") or "").split())
            if text:
                return text if len(text) <= MAX_TITLE else text[: MAX_TITLE - 1].rstrip() + "…"
    return "New chat"


def _path(conv_id: str) -> Path:
    if not valid_id(conv_id):
        raise ValidationError(f"bad conversation id {conv_id!r}")
    return conversations_dir() / f"{conv_id}.json"


def _write(c: dict[str, Any]) -> None:
    path = _path(str(c["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def get(conv_id: str) -> dict[str, Any] | None:
    try:
        path = _path(conv_id)
    except ValidationError:
        return None
    try:
        c = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return c if isinstance(c, dict) else None


def summary(c: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    keys = ("id", "title", "created_at", "updated_at", "kept_at", "archived_at")
    return {k: c.get(k) for k in keys} | {
        "messages": len(c.get("messages") or []),
        "archived": is_archived(c, now),
    }


def save(conv_id: str, messages: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """Create or replace a conversation's messages. `updated_at` moves only when the list
    grew or changed, so re-saving the same list (a reload) isn't activity."""
    if not isinstance(messages, list) or not messages:
        raise ValidationError("a conversation needs at least one message")
    if len(json.dumps(messages)) > MAX_BYTES:
        raise ValidationError("the conversation is too large to keep (8 MB)")
    now = time.time() if now is None else now
    old = get(conv_id) or {}
    _path(conv_id)  # checks the id
    changed = old.get("messages") != messages
    c = {
        "id": conv_id,
        "title": title_of(messages),
        "created_at": old.get("created_at") or now,
        "updated_at": now if changed or not old else old.get("updated_at"),
        "kept_at": old.get("kept_at"),
        "archived_at": old.get("archived_at"),
        "messages": messages,
    }
    if changed or not old:
        _write(c)
    return c


def set_archived(conv_id: str, archived: bool, now: float | None = None) -> dict[str, Any]:
    c = get(conv_id)
    if c is None:
        raise KeyError(conv_id)
    now = time.time() if now is None else now
    if archived:
        c["archived_at"] = now
    else:
        c["archived_at"] = None
        c["kept_at"] = now  # back in the main list for another ARCHIVE_DAYS
    _write(c)
    return c


def _text(c: dict[str, Any]) -> str:
    parts = [str(c.get("title") or "")]
    for m in c.get("messages") or []:
        parts.append(str(m.get("content") or ""))
    return "\n".join(parts).lower()


def list_all(query: str = "", now: float | None = None) -> list[dict[str, Any]]:
    """Summaries, newest message first; `query` matches the title and every message."""
    d = conversations_dir()
    if not d.is_dir():
        return []
    q = query.strip().lower()
    out = []
    for p in d.glob("*.json"):
        if not valid_id(p.stem):
            continue
        c = get(p.stem)
        if c is None or (q and q not in _text(c)):
            continue
        out.append(summary(c, now))
    out.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return out
