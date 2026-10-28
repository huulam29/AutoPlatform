"""Structured logging: text or JSON lines, with run/node context and secret redaction.

A record carries context when it was logged with `extra={"run_id": ..., "node_id": ...,
"iteration": ...}` (StepContext's `ctx.log` adapter does this for step code; the worker and
scheduler pass it too). JSON lines look like:

    {"ts": "2026-10-18T09:12:03.123Z", "level": "INFO", "logger": "agentline.worker",
     "msg": "...", "run_id": "r01...", "node_id": "plan", "iteration": 1}
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from typing import IO, Any

from agentline.redact import Redactor

CONTEXT_FIELDS = ("run_id", "node_id", "iteration", "execution_id", "worker_id")
_MARK = "_agentline_handler"


def _context(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k in CONTEXT_FIELDS if (v := getattr(record, k, None)) is not None}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        out: dict[str, Any] = {
            "ts": f"{ts}.{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_context(record),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            out["exc"] = record.exc_text
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        ctx = _context(record)
        if not ctx:
            return text
        where = ctx.get("run_id", "")
        if "node_id" in ctx:
            where += f" {ctx['node_id']}" + (f"#{ctx['iteration']}" if "iteration" in ctx else "")
        first, nl, rest = text.partition("\n")
        return f"{first} [{where.strip()}]{nl}{rest}"


class RedactFilter(logging.Filter):
    """Redacts the formatted message (and any exception text) of every record it sees."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.redactor:
            return True
        record.msg = self.redactor.redact(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = self.redactor.redact(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.redactor.redact(record.exc_text)
        return True


class RingHandler(logging.Handler):
    """The last `size` log lines in memory (redacted, text format), for GET /api/logs."""

    def __init__(self, size: int = 2000) -> None:
        super().__init__()
        import collections

        self.lines: collections.deque[str] = collections.deque(maxlen=size)
        self.setFormatter(TextFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):  # never let logging break the engine
            self.lines.append(self.format(record))


RING = RingHandler(size=5000)


class QuietAccessFilter(logging.Filter):
    """Drops uvicorn access lines for successful reads (GET/HEAD with a status below 400): the
    app and the UI poll every few seconds, and those lines buried everything else. Errors,
    writes (POST/PUT/DELETE) and other loggers pass. `AGENTLINE_ACCESS_LOG=all` keeps every line."""

    def filter(self, record: logging.LogRecord) -> bool:
        if os.environ.get("AGENTLINE_ACCESS_LOG", "").lower() == "all":
            return True
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        method, status = str(args[1]).upper(), args[4]
        try:
            ok = int(status) < 400
        except (TypeError, ValueError):
            return True
        return not (ok and method in ("GET", "HEAD"))


def quiet_access_log() -> None:
    """Install QuietAccessFilter on `uvicorn.access` once (a logger filter, so it applies to
    every handler, uvicorn's own included)."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, QuietAccessFilter) for f in access.filters):
        access.addFilter(QuietAccessFilter())


def setup_logging(
    format: str = "text",
    level: str | int = "INFO",
    *,
    redactor: Redactor | None = None,
    stream: IO[str] | None = None,
) -> logging.Handler:
    """Install one handler on the root logger (replacing a previous one from this function).
    `format` is `text` or `json`; every record passes the redaction filter first."""
    if format not in ("text", "json"):
        raise ValueError(f"log format must be text or json, not {format!r}")
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _MARK, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    setattr(handler, _MARK, True)
    handler.setFormatter(JsonFormatter() if format == "json" else TextFormatter())
    handler.addFilter(RedactFilter(redactor if redactor is not None else Redactor.from_env()))
    root.addHandler(handler)
    quiet_access_log()
    # the same lines, kept for the Logs page; its redaction follows the latest setup (new keys)
    for f in [f for f in RING.filters if isinstance(f, RedactFilter)]:
        RING.removeFilter(f)
    RING.addFilter(RedactFilter(redactor if redactor is not None else Redactor.from_env()))
    if RING not in root.handlers:
        root.addHandler(RING)
    root.setLevel(level if isinstance(level, int) else level.upper())
    return handler
