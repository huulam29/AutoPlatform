"""KafkaSource: turn each Kafka message into exactly one agentline run.

- Run identity is the message's coordinates: `dedupe_key = "kafka:{topic}:{partition}:{offset}"`,
  so a redelivered message (after a crash or a rebalance) resubmits as a no-op.
- Offsets are committed manually and only contiguously: offset N+1 is committed once every
  message up to N that this consumer received in the partition has a terminal run (`done`,
  `dead`, `cancelled`). `waiting` and `needs-review` are not terminal, so they hold the offset.
- A partition is paused while its window of uncommitted messages is full, or while its oldest
  uncommitted run is long-running (waiting on a gate or a human), and resumed when that clears.
  The consumer keeps polling the other partitions (and the group heartbeat stays alive).
- A message whose payload function raises (a poison message) becomes a `dead` run carrying the
  error, so it is visible and its offset can still be committed.

Delivery from Kafka is at-least-once; run creation is exactly-once (by `dedupe_key`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agentline.ids import new_id
from agentline.ledger.base import Run
from agentline.sources.consumer import Consumer, Message, TopicPartition
from agentline.states import TERMINAL_RUN, RunStatus

if TYPE_CHECKING:
    from agentline.app import App

log = logging.getLogger("agentline.sources.kafka")

PayloadFn = Callable[[Message], Any]
LONG_RUNNING = frozenset({RunStatus.WAITING, RunStatus.NEEDS_REVIEW})
POISON_PREVIEW = 2048


def dedupe_key(topic: str, partition: int, offset: int) -> str:
    return f"kafka:{topic}:{partition}:{offset}"


def json_payload(msg: Message) -> Any:
    """Default payload: the message value parsed as JSON."""
    if msg.value is None:
        raise ValueError("message has no value")
    return json.loads(msg.value)


@dataclass
class _Entry:
    run_id: str
    status: str


@dataclass
class _Partition:
    # delivered, not yet committable, in delivery order: offset -> entry
    window: OrderedDict[int, _Entry] = field(default_factory=OrderedDict)
    commit_point: int | None = None  # next offset to commit (everything before is terminal)
    committed: int | None = None  # last offset actually committed by us
    paused: bool = False


class KafkaSource:
    def __init__(
        self,
        app: App,
        consumer: Consumer,
        *,
        pipeline: str,
        topics: Iterable[str],
        payload: PayloadFn = json_payload,
        max_in_flight_per_partition: int = 16,
        commit_interval: float = 1.0,
        refresh_interval: float = 0.2,
        pause_on: Iterable[str] = LONG_RUNNING,
        poll_timeout: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`pipeline`: the flow name runs are submitted to. `max_in_flight_per_partition`
        bounds the uncommitted window per partition (and so the redelivery after a crash).
        `commit_interval`: seconds between offset commits (0 = commit whenever possible).
        `pause_on`: run statuses that, on the oldest uncommitted run, pause the partition."""
        if max_in_flight_per_partition < 1:
            raise ValueError("max_in_flight_per_partition must be >= 1")
        self.app = app
        self.consumer = consumer
        self.pipeline = pipeline
        self.topics = list(topics)
        self.payload = payload
        self.max_in_flight = max_in_flight_per_partition
        self.commit_interval = commit_interval
        self.refresh_interval = refresh_interval
        self.pause_on = frozenset(str(s) for s in pause_on)
        self.poll_timeout = poll_timeout
        self.clock = clock
        self.parts: dict[TopicPartition, _Partition] = {}
        self._last_commit = float("-inf")
        self._last_refresh = float("-inf")
        self._subscribed = False
        self.counters = {"messages": 0, "created": 0, "redelivered": 0, "poison": 0, "commits": 0}

    # -- lifecycle --------------------------------------------------------------------------
    def subscribe(self) -> None:
        if not self._subscribed:
            self.consumer.subscribe(
                self.topics, on_assign=self._on_assign, on_revoke=self._on_revoke
            )
            self._subscribed = True

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Poll until `stop` is set; commits what it can on the way out."""
        stop = stop or asyncio.Event()
        self.subscribe()
        try:
            while not stop.is_set():
                await asyncio.to_thread(self.poll_once)
        finally:
            await asyncio.to_thread(self.maintain, True)

    def poll_once(self, timeout: float | None = None) -> Message | None:
        """One poll + handle + housekeeping. Synchronous; rebalance callbacks fire in here."""
        self.subscribe()
        msg = self.consumer.poll(self.poll_timeout if timeout is None else timeout)
        if msg is not None:
            self.handle(msg)
        self.maintain()
        return msg

    # -- messages ---------------------------------------------------------------------------
    def handle(self, msg: Message) -> str | None:
        """Submit (or find) the run for `msg` and track it. Returns the run id."""
        tp = msg.tp
        part = self.parts.setdefault(tp, _Partition())
        if msg.offset in part.window or (
            part.commit_point is not None and msg.offset < part.commit_point
        ):
            return None  # duplicate delivery within this assignment
        self.counters["messages"] += 1
        key = dedupe_key(msg.topic, msg.partition, msg.offset)
        try:
            payload = self.payload(msg)
        except Exception as e:
            run_id, status, created = self._poison(msg, key, e)
        else:
            sub = self.app.submit_sync(self.pipeline, payload, dedupe_key=key)
            run_id, created = sub.run_id, sub.created
            status = self._status(run_id)
        self.counters["created" if created else "redelivered"] += 1
        part.window[msg.offset] = _Entry(run_id, status)
        self._advance(tp, part)
        self._update_pause(tp, part)
        return run_id

    def _poison(self, msg: Message, key: str, err: Exception) -> tuple[str, str, bool]:
        error = f"poison message: {type(err).__name__}: {err}"
        log.warning("%s: %s", key, error)
        app = self.app
        with app.ledger.tx() as tx:
            existing = tx.get_run_by_dedupe(key)
            if existing is not None:
                return existing.id, existing.status, False
            self.counters["poison"] += 1
            value = (msg.value or b"")[:POISON_PREVIEW].decode("utf-8", "replace")
            payload = app.redactor.redact_value(
                {
                    "kafka": {"topic": msg.topic, "partition": msg.partition, "offset": msg.offset},
                    "key": (msg.key or b"").decode("utf-8", "replace") or None,
                    "value_preview": value,
                }
            )
            now = time.time()
            graph = app.graph(self.pipeline)
            run = Run(
                id=new_id("r"),
                flow=self.pipeline,
                flow_hash=graph.structure_hash(),
                payload=payload,
                status=RunStatus.DEAD,
                dedupe_key=key,
                error=app.redactor.redact_value(error),
                created_at=now,
                updated_at=now,
            )
            tx.insert_run(run)
            tx.add_audit(
                "kafka-source",
                "poison",
                run.id,
                None,
                app.redactor.redact_value({"dedupe_key": key, "error": error}),
                now,
            )
        return run.id, RunStatus.DEAD, True

    def _status(self, run_id: str) -> str:
        with self.app.ledger.tx() as tx:
            run = tx.get_run(run_id)
        return run.status if run else RunStatus.DEAD

    # -- housekeeping -----------------------------------------------------------------------
    def maintain(self, force: bool = False) -> None:
        """Refresh run statuses, advance commit points, pause/resume, commit if due."""
        now = self.clock()
        if force or now - self._last_refresh >= self.refresh_interval:
            self._last_refresh = now
            self.refresh()
        if force or now - self._last_commit >= self.commit_interval:
            self.commit()

    def refresh(self, partitions: Iterable[TopicPartition] | None = None) -> None:
        tps = list(self.parts if partitions is None else partitions)
        open_ids = {
            e.run_id
            for tp in tps
            if tp in self.parts
            for e in self.parts[tp].window.values()
            if e.status not in TERMINAL_RUN
        }
        if open_ids:
            with self.app.ledger.tx() as tx:
                statuses = {rid: (r.status if (r := tx.get_run(rid)) else None) for rid in open_ids}
        else:
            statuses = {}
        for tp in tps:
            part = self.parts.get(tp)
            if part is None:
                continue
            for e in part.window.values():
                if (s := statuses.get(e.run_id)) is not None:
                    e.status = s
            self._advance(tp, part)
            self._update_pause(tp, part)

    @staticmethod
    def _advance(tp: TopicPartition, part: _Partition) -> None:
        while part.window:
            offset, entry = next(iter(part.window.items()))
            if entry.status not in TERMINAL_RUN:
                break
            part.window.popitem(last=False)
            part.commit_point = offset + 1

    def _update_pause(self, tp: TopicPartition, part: _Partition) -> None:
        head = next(iter(part.window.values()), None)
        hold = len(part.window) >= self.max_in_flight or (
            head is not None and head.status in self.pause_on
        )
        if hold and not part.paused:
            self.consumer.pause([tp])
            part.paused = True
            log.info("%s: paused (%d uncommitted)", tp, len(part.window))
        elif not hold and part.paused:
            self.consumer.resume([tp])
            part.paused = False
            log.info("%s: resumed", tp)

    def commit(
        self, partitions: Iterable[TopicPartition] | None = None
    ) -> dict[TopicPartition, int]:
        """Commit every partition whose commit point moved. Returns what was committed."""
        self._last_commit = self.clock()
        tps = self.parts if partitions is None else partitions
        due = {
            tp: p.commit_point
            for tp in tps
            if (p := self.parts.get(tp)) is not None
            and p.commit_point is not None
            and p.commit_point != p.committed
        }
        if not due:
            return {}
        self.consumer.commit(due)
        for tp, off in due.items():
            self.parts[tp].committed = off
        self.counters["commits"] += 1
        return due

    # -- rebalance --------------------------------------------------------------------------
    def _on_assign(self, partitions: list[TopicPartition]) -> None:
        for tp in partitions:
            self.parts[tp] = _Partition()  # fresh: position comes from the committed offset
        log.info("assigned %s", ", ".join(map(str, partitions)))

    def _on_revoke(self, partitions: list[TopicPartition]) -> None:
        """Commit what is final for the revoked partitions, then forget them. Their open runs
        keep going; the next owner gets the same runs back through dedupe_key."""
        mine = [tp for tp in partitions if tp in self.parts]
        try:
            self.refresh(mine)
            self.commit(mine)
        except Exception as e:  # the partition may already be gone (lost); redelivery is safe
            log.warning("commit on revoke failed: %s", e)
        for tp in mine:
            del self.parts[tp]
        log.info("revoked %s", ", ".join(map(str, partitions)))

    # -- introspection ----------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            **self.counters,
            "partitions": {
                str(tp): {
                    "uncommitted": len(p.window),
                    "committed": p.committed,
                    "commit_point": p.commit_point,
                    "paused": p.paused,
                }
                for tp, p in sorted(self.parts.items())
            },
        }
