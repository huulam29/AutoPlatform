"""Crash and failure reports ("incidents"), stored locally so they can be fixed later.

One JSON file per distinct problem in `$AGENTLINE_HOME/incidents/<id>.json`, shared with the
Mac app (which writes `source: "app"` records in the same format). The same problem again
(same fingerprint) updates the file: `count` goes up, `last_seen` moves, the latest message and
traceback replace the old ones, `status` goes back to `new`. Nothing is ever uploaded; every
text field passes through the redactor before it is written.

Kinds: `engine_exception` (an unhandled exception in the engine process), `api_error` (an HTTP
500 from the API), `worker_error` (a bug-like error in a step or the worker loop), and the
app's `app_crash`, `app_exception`, `app_unclean_exit`, `engine_exit`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import time
import traceback as tb
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agentline.filelock import locked_file

KINDS = (
    "engine_exception",
    "api_error",
    "worker_error",
    "engine_exit",
    "engine_unclean_exit",  # serve's start marker was still there (agentline.runmarker)
    "app_crash",
    "app_exception",
    "app_unclean_exit",
)
# Exceptions that mean "agentline (or a plugin) has a bug", as opposed to a step failing for a
# reason the pipeline already handles (a runner error, an API error, a timeout, a bad config).
BUG_TYPES = (
    AttributeError,
    AssertionError,
    ImportError,
    IndexError,
    KeyError,
    NameError,
    NotImplementedError,
    RecursionError,
    TypeError,
    UnboundLocalError,
    ZeroDivisionError,
)
MAX_TEXT = 20_000
_ID = re.compile(r"^[0-9a-f]{16}$")
_NORMALIZE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "<id>"),
    (re.compile(r"\b(?:r|x)[0-9a-f]{16,}\b"), "<id>"),
    (re.compile(r"\d+"), "N"),
]


def incidents_dir() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "incidents"


def platform_text() -> str:
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]} {platform.machine()}"
    return f"{platform.system()} {platform.release()} {platform.machine()}"


def normalize(message: str) -> str:
    out = message.strip().splitlines()[0][:300] if message.strip() else ""
    for pattern, repl in _NORMALIZE:
        out = pattern.sub(repl, out)
    return out


def top_frames(exc: BaseException, n: int = 3) -> list[str]:
    """The innermost frames as `file:function` (no line numbers, so edits don't split them)."""
    frames = tb.extract_tb(exc.__traceback__)[-n:]
    return [f"{Path(f.filename).name}:{f.name}" for f in frames]


def fingerprint(
    source: str, kind: str, exception_type: str | None, message: str, frames: list[str]
) -> str:
    key = "|".join([source, kind, exception_type or "", normalize(message), ",".join(frames)])
    return hashlib.sha256(key.encode()).hexdigest()


def is_bug(exc: BaseException) -> bool:
    return isinstance(exc, BUG_TYPES)


class IncidentStore:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        redactor: Any = None,
        version: str | None = None,
        source: str = "engine",
    ) -> None:
        self.root = Path(root) if root is not None else incidents_dir()
        self.redactor = redactor
        self.source = source
        if version is None:
            from agentline import __version__

            version = __version__
        self.version = version

    # -- writing ----------------------------------------------------------------------------
    def _redact(self, value: Any) -> Any:
        if self.redactor is None:
            return value
        if isinstance(value, str):
            return self.redactor.redact(value)
        return self.redactor.redact_value(value)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with locked_file(self.root / ".lock"):
            yield

    def record(
        self,
        kind: str,
        *,
        title: str,
        message: str = "",
        exception_type: str | None = None,
        traceback: str = "",
        frames: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update the incident for this problem. Never raises: reporting a crash
        must not cause one (failures are written to stderr instead)."""
        try:
            return self._record(kind, title, message, exception_type, traceback, frames, context)
        except Exception as e:  # pragma: no cover - last-resort guard
            print(f"agentline: couldn't record incident ({type(e).__name__}: {e})", file=sys.stderr)
            return {}

    def _record(
        self,
        kind: str,
        title: str,
        message: str,
        exception_type: str | None,
        traceback: str,
        frames: list[str] | None,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        title = str(self._redact(title))[:300]
        message = str(self._redact(message))[:MAX_TEXT]
        traceback = str(self._redact(traceback))[-MAX_TEXT:]
        context = self._redact(dict(context or {}))
        fp = fingerprint(self.source, kind, exception_type, message, frames or [])
        iid = fp[:16]
        now = time.time()
        with self._locked():
            path = self.root / f"{iid}.json"
            old = self._read(path)
            record = {
                "id": iid,
                "fingerprint": fp,
                "source": self.source,
                "kind": kind,
                "title": title,
                "message": message,
                "exception_type": exception_type,
                "traceback": traceback,
                "context": context,
                "first_seen": old.get("first_seen", now) if old else now,
                "last_seen": now,
                "count": int(old.get("count", 0)) + 1 if old else 1,
                "version": self.version,
                "platform": platform_text(),
                "status": "new",
                "attachments": old.get("attachments", []) if old else [],
            }
            self._write(path, record)
        return record

    def record_exception(
        self, kind: str, exc: BaseException, *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        text = "".join(tb.format_exception(type(exc), exc, exc.__traceback__))
        name = type(exc).__name__
        msg = str(exc)
        where = top_frames(exc, 1)
        title = f"{name}: {msg.splitlines()[0][:120] if msg else ''}".rstrip(": ")
        if where:
            title += f" (in {where[0]})"
        return self.record(
            kind,
            title=title,
            message=msg,
            exception_type=name,
            traceback=text,
            frames=top_frames(exc),
            context=context,
        )

    def _write(self, path: Path, record: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".incident.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, default=str)
        os.replace(tmp, path)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    # -- reading ----------------------------------------------------------------------------
    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        """Summaries (no traceback/context), newest first."""
        if not self.root.exists():
            return []
        items = [self._read(p) for p in self.root.glob("*.json")]
        items = [i for i in items if i.get("id")]
        items.sort(key=lambda i: i.get("last_seen", 0), reverse=True)
        skip = {"traceback", "context"}
        return [{k: v for k, v in i.items() if k not in skip} for i in items[:limit]]

    def get(self, iid: str) -> dict[str, Any] | None:
        if not _ID.match(iid):
            return None
        rec = self._read(self.root / f"{iid}.json")
        return rec or None

    def mark_seen(self, iid: str) -> bool:
        if not _ID.match(iid):
            return False
        with self._locked():
            path = self.root / f"{iid}.json"
            rec = self._read(path)
            if not rec:
                return False
            rec["status"] = "seen"
            self._write(path, rec)
        return True

    def delete(self, iid: str) -> bool:
        if not _ID.match(iid):
            return False
        with self._locked():
            path = self.root / f"{iid}.json"
            if not path.exists():
                return False
            path.unlink()
            att = self.root / iid
            if att.is_dir():
                for p in sorted(att.rglob("*"), reverse=True):
                    p.unlink() if p.is_file() else p.rmdir()
                att.rmdir()
        return True

    def clear(self) -> int:
        n = 0
        for rec in self.list(limit=100_000):
            n += self.delete(rec["id"])
        return n

    def new_count(self) -> int:
        return sum(1 for i in self.list(limit=100_000) if i.get("status") == "new")


def install_process_hooks(store: IncidentStore) -> None:
    """Record unhandled exceptions in the main thread and in other threads."""
    previous = sys.excepthook

    def hook(etype: type[BaseException], value: BaseException, trace: Any) -> None:
        if not isinstance(value, KeyboardInterrupt):
            store.record_exception("engine_exception", value, context={"where": "main thread"})
        previous(etype, value, trace)

    sys.excepthook = hook

    import threading

    previous_thread = threading.excepthook

    def thread_hook(args: Any) -> None:
        if args.exc_value is not None:
            store.record_exception(
                "engine_exception",
                args.exc_value,
                context={"where": f"thread {getattr(args.thread, 'name', '?')}"},
            )
        previous_thread(args)

    threading.excepthook = thread_hook


def asyncio_handler(store: IncidentStore) -> Any:
    """For `loop.set_exception_handler`: record exceptions nobody awaited."""

    def handle(loop: Any, ctx: dict[str, Any]) -> None:
        exc = ctx.get("exception")
        if isinstance(exc, BaseException):
            store.record_exception(
                "engine_exception", exc, context={"where": "asyncio", "message": ctx.get("message")}
            )
        else:
            store.record(
                "engine_exception",
                title=str(ctx.get("message") or "asyncio error"),
                message=str(ctx.get("message") or ""),
                context={"where": "asyncio"},
            )
        loop.default_exception_handler(ctx)

    return handle
