"""Fallback for missed webhooks: poll waiting `wait` nodes that set `poll_every`.

A wait node parks with `detail = {kind: wait, waiter, params, poll_every}`. When its
waiter has a `poll(params, inputs)` method (optionally taking `since=`, the wait
execution's creation time, so it can skip older results), this loop calls it every
`poll_every` seconds per execution and delivers a non-None result as the event on the
execution's correlation key, exactly as a webhook would. A payload with an `event_id` is
delivered at most once across webhook and poller (agentline.deliveries), so a late webhook
for a review the poller already delivered can't resume the *next* iteration's wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agentline.app import App
from agentline.deliveries import deliver_once
from agentline.ledger.base import Execution
from agentline.registry import Registry

log = logging.getLogger("agentline.poller")


@dataclass
class PollState:
    last_poll: dict[str, float] = field(default_factory=dict)  # execution id -> time


def _waiting(app: App) -> list[Execution]:
    with app.ledger.tx() as tx:
        conn = getattr(tx, "c", None)
        if conn is not None:  # SqliteTx: one indexed query over all runs
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM executions WHERE status = 'waiting'"
                ).fetchall()
            ]
            found = [tx.get_execution(i) for i in ids]
            return [x for x in found if x is not None]
        out: list[Execution] = []  # any other backend: through the protocol
        for status in ("waiting", "running", "pending"):
            for run in tx.list_runs(status, limit=10_000):
                out.extend(x for x in tx.list_executions(run.id) if x.status == "waiting")
        return out


def _accepts_since(fn: Any) -> bool:
    try:
        return "since" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


async def poll_once(
    app: App, registry: Registry, state: PollState | None = None, now: float | None = None
) -> list[str]:
    """One scan. Returns the correlation keys it delivered events for."""
    state = state or PollState()
    now = time.time() if now is None else now
    execs = await asyncio.to_thread(_waiting, app)
    live = {x.id for x in execs}
    for gone in set(state.last_poll) - live:
        del state.last_poll[gone]
    delivered: list[str] = []
    for ex in execs:
        d = ex.detail or {}
        every, waiter_name = d.get("poll_every"), d.get("waiter")
        if d.get("kind") != "wait" or not every or not waiter_name or not ex.correlation_key:
            continue
        since = state.last_poll.get(ex.id, ex.started_at or ex.created_at)
        if now < since + float(every):
            continue
        waiter = registry.waiters.get(waiter_name)
        poll = getattr(waiter, "poll", None)
        if poll is None:
            continue
        state.last_poll[ex.id] = now
        try:
            inputs = await asyncio.to_thread(_inputs, app, ex)
            kw = {"since": ex.created_at} if _accepts_since(poll) else {}
            payload = poll(d.get("params") or {}, inputs, **kw)
            if inspect.isawaitable(payload):
                payload = await payload
        except Exception as e:  # a failed poll is retried next interval
            log.warning("poll %s for %s failed: %s", waiter_name, ex.key, e)
            continue
        if payload is None:
            continue
        woken = await asyncio.to_thread(_deliver, app, ex.correlation_key, payload)
        if woken is None:
            continue  # already delivered (by the webhook) under the same event_id
        log.info("poll %s delivered on %s (woke %s)", waiter_name, ex.correlation_key, woken)
        delivered.append(ex.correlation_key)
    return delivered


def _deliver(app: App, key: str, payload: Any) -> list[str] | None:
    with app.ledger.tx() as tx:
        return deliver_once(app, tx, key, payload)


def _inputs(app: App, ex: Execution) -> dict[str, Any]:
    with app.ledger.tx() as tx:
        return app.scheduler.resolve_inputs(tx, ex)


async def run_poller(
    app: App, registry: Registry, stop: asyncio.Event, interval: float = 30.0
) -> None:
    """Scan every `interval` seconds until `stop` is set."""
    state = PollState()
    while not stop.is_set():
        try:
            await poll_once(app, registry, state)
        except Exception:
            log.exception("poller scan failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval)
