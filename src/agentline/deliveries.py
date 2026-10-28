"""Seen webhook delivery ids and event ids, for replay rejection and webhook/poller dedupe.

The `webhook_deliveries` table is part of every ledger's schema (SQLite and Postgres) and is
written through `LedgerTx.claim_delivery`, in the same transaction as the event it protects,
so a failed delivery leaves no claim behind and the sender's retry still works."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentline.app import App


def ensure_table(tx: Any) -> None:
    """Kept for callers from Phase 4: the table now ships with each ledger's schema."""
    if not hasattr(tx, "claim_delivery"):
        raise RuntimeError(
            f"webhook replay protection needs a ledger with claim_delivery ({type(tx).__name__})"
        )


def claim_delivery(tx: Any, source: str, delivery_id: str) -> bool:
    """Record a delivery id; False if it was seen before (a replay)."""
    ensure_table(tx)
    return bool(tx.claim_delivery(source, delivery_id, time.time()))


def deliver_once(app: App, tx: Any, correlation_key: str, payload: Any) -> list[str] | None:
    """`deliver_event` inside `tx`, unless the payload's `event_id` was delivered before
    (then None). Payloads without an `event_id` are always delivered."""
    event_id = payload.get("event_id") if isinstance(payload, dict) else None
    if event_id and not claim_delivery(tx, "event", str(event_id)):
        return None
    woken: list[str] = app.scheduler.deliver_event(tx, correlation_key, payload)
    return woken
