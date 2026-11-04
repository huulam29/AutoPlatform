"""Unclean-exit detection for `agentline serve` (spec 006 FR-014).

`serve` writes `<home>/engine.run.json` once it holds the data folder lock and removes it after
a normal stop (a signal, the stop event, a restart). A marker still there at the next start
means the last engine was killed, crashed or lost power: `check_previous` records one
`engine_unclean_exit` incident from it. Inside the Mac app (`AGENTLINE_PARENT_PID` set) all of
this is skipped: the app keeps its own marker and `app_unclean_exit` reports.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

MARKER_FILE = "engine.run.json"
KIND = "engine_unclean_exit"


def in_mac_app() -> bool:
    return bool(os.environ.get("AGENTLINE_PARENT_PID", "").isdigit())


def marker_path(home: str | Path) -> Path:
    return Path(home) / MARKER_FILE


def write(home: str | Path, address: str) -> None:
    if in_mac_app():
        return
    from agentline import __version__
    from agentline.incidents import platform_text

    path = marker_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "pid": os.getpid(),
        "started_at": time.time(),
        "version": __version__,
        "platform": platform_text(),
        "address": address,
    }
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def remove(home: str | Path) -> None:
    if in_mac_app():
        return
    with contextlib.suppress(FileNotFoundError):
        marker_path(home).unlink()


def check_previous(home: str | Path, store: Any) -> dict[str, Any] | None:
    """One incident for a marker the last engine left behind (then the marker is removed);
    None when the last stop was clean."""
    if in_mac_app():
        return None
    path = marker_path(home)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    try:
        prev = json.loads(raw)
        if not isinstance(prev, dict):
            prev = {}
    except ValueError:
        prev = {}
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    if store is None:
        return None
    started = prev.get("started_at")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) if started else "unknown"
    return store.record(
        KIND,
        title="agentline engine quit unexpectedly",
        message=f"The engine started at {when} stopped without shutting down cleanly"
        " (killed, crashed, or the machine lost power).",
        context={
            "previous_pid": prev.get("pid"),
            "started_at": started,
            "version": prev.get("version"),
            "platform": prev.get("platform"),
            "address": prev.get("address"),
        },
    )
