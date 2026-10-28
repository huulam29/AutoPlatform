""" "Settings changed since the engine started": running workers keep the config they started
with, so any settings write (Settings API, the assistant from chat, an import that adds
settings) notes it here and `/api/status` reports it; the Mac app shows Restart Engine."""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_pending: dict[str, float] = {}


def note(reason: str) -> None:
    with _lock:
        _pending[reason] = time.time()


def pending() -> list[str]:
    with _lock:
        return sorted(_pending, key=_pending.__getitem__)


def clear() -> None:
    with _lock:
        _pending.clear()
