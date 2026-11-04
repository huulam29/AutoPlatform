"""People's feedback on runs and node outputs (phase 58).

A rating (`up`, `down`, or `none` to withdraw one) with an optional comment on a run, or on one
node and iteration of it, is a journal row of kind `feedback`:
`detail = {"rating": "up" | "down" | None, "comment": str, "by": str}` (node / iteration in the
row's own columns; the run itself when node is None). Comments are redacted before the insert.
Nothing is ever changed: a person's newest row for a target is their current rating.

A thumbs-down or any comment marks the run for a review (agentline.review reads
`<home>/reviews/feedback-pending.json`, `{"runs": {run_id: {"id": newest entry id, "at": ts}}}`),
so feedback given while `serve` is down is picked up later.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError

RATINGS = ("up", "down", "none")
MAX_COMMENT = 2000
KIND = "feedback"
_lock = threading.Lock()


def pending_path() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "reviews" / "feedback-pending.json"


def read_pending() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(pending_path().read_text())
        runs = data.get("runs") if isinstance(data, dict) else None
        return dict(runs) if isinstance(runs, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_pending(runs: dict[str, dict[str, Any]]) -> None:
    p = pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".pending.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump({"runs": runs}, f, indent=2)
    os.replace(tmp, p)


def mark_pending(run_id: str, entry_id: int, at: float) -> None:
    with _lock:
        runs = read_pending()
        old = runs.get(run_id) or {}
        runs[run_id] = {"id": max(int(old.get("id") or 0), entry_id), "at": at}
        _write_pending(runs)


def clear_pending(run_id: str, upto: int) -> None:
    """Forget a run's pending feedback up to entry `upto` (newer entries stay pending)."""
    with _lock:
        runs = read_pending()
        cur = runs.get(run_id)
        if cur is not None and int(cur.get("id") or 0) <= upto:
            runs.pop(run_id, None)
            _write_pending(runs)


def is_negative(entry: dict[str, Any]) -> bool:
    return entry.get("rating") == "down" or bool(str(entry.get("comment") or "").strip())


def _entry(j: Any) -> dict[str, Any]:
    d = j.detail or {}
    return {
        "id": j.id,
        "at": j.at,
        "run": j.run_id,
        "node": j.node_id,
        "iteration": j.iteration,
        "rating": d.get("rating"),
        "comment": str(d.get("comment") or ""),
        "by": str(d.get("by") or "unknown"),
    }


def add(
    app: Any,
    run_id: str,
    rating: str,
    comment: str = "",
    *,
    node: str | None = None,
    iteration: int | None = None,
    by: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Record one rating; returns the entry. ValidationError on bad input, KeyError on no run."""
    from agentline.app import default_actor

    rating = str(rating or "").strip().lower()
    if rating not in RATINGS:
        raise ValidationError(f"rating must be one of {', '.join(RATINGS)}, not {rating!r}")
    comment = str(comment or "").strip()
    if len(comment) > MAX_COMMENT:
        raise ValidationError(f"a comment is at most {MAX_COMMENT} characters ({len(comment)})")
    comment = app.redactor.redact(comment)
    by = app.redactor.redact(str(by or default_actor()))[:200]
    node = str(node).strip() if node else None
    at = time.time() if now is None else now
    with app.ledger.tx() as tx:
        run = tx.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        execution_id = None
        it = 1
        if node:
            execs = [x for x in tx.list_executions(run_id) if x.node_id == node]
            if not execs:
                raise ValidationError(f"run {run_id} has no node {node!r}")
            it = max(x.iteration for x in execs) if iteration is None else int(iteration)
            match = [x for x in execs if x.iteration == it]
            if not match:
                raise ValidationError(f"node {node!r} has no iteration {it} in run {run_id}")
            execution_id = match[-1].id
        value = None if rating == "none" else rating
        word = {"up": "👍", "down": "👎", None: "withdrawn"}[value]
        target = f"{node} #{it}" if node else "the run"
        message = f"feedback on {target}: {word}" + (f" — {comment[:200]}" if comment else "")
        eid = tx.add_journal(
            run_id,
            KIND,
            message,
            execution_id=execution_id,
            node_id=node,
            iteration=it,
            detail={"rating": value, "comment": comment, "by": by},
            at=at,
        )
        finished = run.status in ("done", "dead", "cancelled")
    entry = {
        "id": eid,
        "at": at,
        "run": run_id,
        "node": node,
        "iteration": it,
        "rating": value,
        "comment": comment,
        "by": by,
        "run_finished": finished,
    }
    if is_negative(entry):
        mark_pending(run_id, eid, at)
    return entry


def entries(app: Any, run_id: str, tx: Any = None) -> list[dict[str, Any]]:
    """Every feedback row of a run, oldest first."""
    if tx is None:
        with app.ledger.tx() as t:
            return entries(app, run_id, t)
    return [_entry(j) for j in tx.list_journal(run_id) if j.kind == KIND]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Current ratings (newest per person and target) and their counts."""
    current: dict[tuple[str, str | None, int], dict[str, Any]] = {}
    for e in rows:
        key = (e["by"], e["node"], int(e["iteration"] if e["node"] else 0))
        current[key] = e  # the newest wins; rating None ("none") withdraws
    cur = sorted(current.values(), key=lambda e: e["id"])
    out: dict[str, Any] = {
        "up": 0,
        "down": 0,
        "comments": sum(1 for e in rows if e["comment"]),
        "run": {"up": 0, "down": 0},
        "nodes": {},
        "current": cur,
    }
    for e in cur:
        if e["rating"] not in ("up", "down"):
            continue
        out[e["rating"]] += 1
        bucket = (
            out["nodes"].setdefault(e["node"], {"up": 0, "down": 0}) if e["node"] else out["run"]
        )
        bucket[e["rating"]] += 1
    return out


def summary(app: Any, run_id: str, tx: Any = None) -> dict[str, Any]:
    return summarize(entries(app, run_id, tx))


def text(rows: list[dict[str, Any]], limit: int = 40) -> list[str]:
    """Feedback in words (review evidence, reports, chat)."""
    out = []
    for e in rows[-limit:]:
        word = {"up": "thumbs up", "down": "thumbs down", None: "withdrew a rating"}.get(
            e["rating"], str(e["rating"])
        )
        target = f"{e['node']} #{e['iteration']}" if e["node"] else "the run"
        line = f"{e['by']} on {target}: {word}"
        if e["comment"]:
            line += f": {e['comment']}"
        out.append(line)
    return out


def newest_id(rows: list[dict[str, Any]]) -> int:
    with contextlib.suppress(ValueError):
        return max(int(e["id"]) for e in rows)
    return 0
