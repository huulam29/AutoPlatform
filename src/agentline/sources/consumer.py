"""The consumer interface KafkaSource drives, plus two implementations:

- `ConfluentConsumer`: adapter over `confluent_kafka.Consumer` (optional extra `kafka`,
  imported lazily). Auto-commit and auto offset store are forced off.
- `FakeBroker` / `FakeConsumer`: topics, partitions and committed consumer-group offsets in a
  SQLite file. A consumer process can be SIGKILLed; a fresh consumer resumes from the
  committed offset and so redelivers everything after it (at-least-once, like Kafka).

Offsets follow Kafka's convention: a committed offset is the *next* offset to read.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("agentline.sources")


@dataclass(frozen=True, order=True)
class TopicPartition:
    topic: str
    partition: int

    def __str__(self) -> str:
        return f"{self.topic}:{self.partition}"


@dataclass(frozen=True)
class Message:
    topic: str
    partition: int
    offset: int
    value: bytes | None
    key: bytes | None = None
    headers: list[tuple[str, bytes]] = field(default_factory=list)

    @property
    def tp(self) -> TopicPartition:
        return TopicPartition(self.topic, self.partition)


PartitionsCallback = Callable[[list[TopicPartition]], None]


class Consumer(Protocol):
    """What KafkaSource needs from a Kafka consumer. Rebalance callbacks fire inside `poll`."""

    def subscribe(
        self,
        topics: Iterable[str],
        *,
        on_assign: PartitionsCallback | None = None,
        on_revoke: PartitionsCallback | None = None,
    ) -> None: ...

    def poll(self, timeout: float) -> Message | None: ...

    def commit(self, offsets: Mapping[TopicPartition, int]) -> None:
        """Synchronously commit `offsets` (next offset to read) for the consumer group."""
        ...

    def pause(self, partitions: Iterable[TopicPartition]) -> None: ...
    def resume(self, partitions: Iterable[TopicPartition]) -> None: ...
    def assignment(self) -> set[TopicPartition]: ...
    def close(self) -> None: ...


class KafkaConsumerError(RuntimeError):
    pass


# -- confluent-kafka adapter ------------------------------------------------------------------
def _confluent() -> Any:
    try:
        import confluent_kafka
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "the Kafka source needs confluent-kafka: install the `kafka` extra "
            "(pip install 'agentline[kafka]' or uv sync --extra kafka)"
        ) from e
    return confluent_kafka


class ConfluentConsumer:
    """`Consumer` over `confluent_kafka.Consumer`. `config` is librdkafka config
    (`bootstrap.servers`, `group.id`, ...); offsets are committed only by KafkaSource."""

    def __init__(self, config: Mapping[str, Any], *, consumer: Any = None) -> None:
        self._ck = _confluent()
        conf = {
            "auto.offset.reset": "earliest",
            **config,
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
        }
        self._c = consumer if consumer is not None else self._ck.Consumer(conf)

    def _tps(self, partitions: Iterable[TopicPartition], offsets: Any = None) -> list[Any]:
        out = []
        for tp in partitions:
            if offsets is None:
                out.append(self._ck.TopicPartition(tp.topic, tp.partition))
            else:
                out.append(self._ck.TopicPartition(tp.topic, tp.partition, offsets[tp]))
        return out

    @staticmethod
    def _ours(partitions: Iterable[Any]) -> list[TopicPartition]:
        return [TopicPartition(p.topic, p.partition) for p in partitions]

    def subscribe(
        self,
        topics: Iterable[str],
        *,
        on_assign: PartitionsCallback | None = None,
        on_revoke: PartitionsCallback | None = None,
    ) -> None:
        kw: dict[str, Any] = {}
        if on_assign is not None:
            kw["on_assign"] = lambda _c, ps: on_assign(self._ours(ps))
        if on_revoke is not None:
            kw["on_revoke"] = lambda _c, ps: on_revoke(self._ours(ps))
            kw["on_lost"] = lambda _c, ps: on_revoke(self._ours(ps))
        self._c.subscribe(list(topics), **kw)

    def poll(self, timeout: float) -> Message | None:
        m = self._c.poll(timeout)
        if m is None:
            return None
        err = m.error()
        if err is not None:
            if err.code() == self._ck.KafkaError._PARTITION_EOF:
                return None
            if err.fatal():
                raise KafkaConsumerError(str(err))
            log.warning("kafka consumer error (non-fatal): %s", err)
            return None
        return Message(
            topic=m.topic(),
            partition=m.partition(),
            offset=m.offset(),
            value=m.value(),
            key=m.key(),
            headers=list(m.headers() or []),
        )

    def commit(self, offsets: Mapping[TopicPartition, int]) -> None:
        if offsets:
            self._c.commit(offsets=self._tps(offsets, offsets), asynchronous=False)

    def pause(self, partitions: Iterable[TopicPartition]) -> None:
        if ps := self._tps(partitions):
            self._c.pause(ps)

    def resume(self, partitions: Iterable[TopicPartition]) -> None:
        if ps := self._tps(partitions):
            self._c.resume(ps)

    def assignment(self) -> set[TopicPartition]:
        return set(self._ours(self._c.assignment()))

    def close(self) -> None:
        self._c.close()


# -- fake broker ------------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    topic TEXT PRIMARY KEY,
    partitions INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    topic TEXT NOT NULL,
    partition INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    key BLOB,
    value BLOB,
    deliveries INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (topic, partition, offset)
);
CREATE TABLE IF NOT EXISTS commits (
    grp TEXT NOT NULL,
    topic TEXT NOT NULL,
    partition INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (grp, topic, partition)
);
"""


def _bytes(v: bytes | str | None) -> bytes | None:
    return v.encode() if isinstance(v, str) else v


class FakeBroker:
    """A Kafka stand-in in one SQLite file, safe to share between processes."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self._rr = 0

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # -- admin / producer ---------------------------------------------------------------------
    def create_topic(self, topic: str, partitions: int = 1) -> None:
        self._q(
            "INSERT OR IGNORE INTO topics (topic, partitions) VALUES (?, ?)", (topic, partitions)
        )

    def partitions(self, topic: str) -> int:
        rows = self._q("SELECT partitions FROM topics WHERE topic = ?", (topic,))
        if not rows:
            raise KeyError(f"no topic {topic!r}")
        return int(rows[0][0])

    def produce(
        self,
        topic: str,
        value: bytes | str | None,
        *,
        key: bytes | str | None = None,
        partition: int | None = None,
    ) -> tuple[int, int]:
        """Append a message; returns (partition, offset). Keyed messages go to
        crc32(key) % partitions, unkeyed ones round-robin."""
        n = self.partitions(topic)
        k = _bytes(key)
        if partition is None:
            if k is not None:
                partition = zlib.crc32(k) % n
            else:
                partition, self._rr = self._rr % n, self._rr + 1
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                (nxt,) = self._conn.execute(
                    "SELECT COALESCE(MAX(offset) + 1, 0) FROM messages"
                    " WHERE topic = ? AND partition = ?",
                    (topic, partition),
                ).fetchone()
                self._conn.execute(
                    "INSERT INTO messages (topic, partition, offset, key, value)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (topic, partition, nxt, k, _bytes(value)),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return partition, int(nxt)

    def end_offsets(self, topic: str) -> dict[int, int]:
        """Next offset that would be written, per partition."""
        ends = dict.fromkeys(range(self.partitions(topic)), 0)
        for p, top in self._q(
            "SELECT partition, MAX(offset) + 1 FROM messages WHERE topic = ? GROUP BY partition",
            (topic,),
        ):
            ends[int(p)] = int(top)
        return ends

    def fetch(self, topic: str, partition: int, offset: int) -> Message | None:
        """The first message at or after `offset`; counts a delivery."""
        with self._lock:
            row = self._conn.execute(
                "SELECT offset, key, value FROM messages WHERE topic = ? AND partition = ?"
                " AND offset >= ? ORDER BY offset LIMIT 1",
                (topic, partition, offset),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE messages SET deliveries = deliveries + 1"
                " WHERE topic = ? AND partition = ? AND offset = ?",
                (topic, partition, row[0]),
            )
        return Message(topic, partition, int(row[0]), row[2], row[1])

    def messages(self, topic: str) -> list[dict[str, Any]]:
        rows = self._q(
            "SELECT partition, offset, key, value, deliveries FROM messages WHERE topic = ?"
            " ORDER BY partition, offset",
            (topic,),
        )
        return [
            {"partition": p, "offset": o, "key": k, "value": v, "deliveries": d}
            for p, o, k, v, d in rows
        ]

    # -- consumer groups ----------------------------------------------------------------------
    def commit(self, group: str, offsets: Mapping[TopicPartition, int]) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for tp, off in offsets.items():
                    self._conn.execute(
                        "INSERT INTO commits (grp, topic, partition, offset, at)"
                        " VALUES (?, ?, ?, ?, ?) ON CONFLICT (grp, topic, partition)"
                        " DO UPDATE SET offset = excluded.offset, at = excluded.at",
                        (group, tp.topic, tp.partition, off, now),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def committed(self, group: str, topic: str) -> dict[int, int]:
        rows = self._q(
            "SELECT partition, offset FROM commits WHERE grp = ? AND topic = ?", (group, topic)
        )
        return {int(p): int(o) for p, o in rows}

    def consumer(self, group: str) -> FakeConsumer:
        return FakeConsumer(self, group)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class FakeConsumer:
    """One member of a consumer group on a FakeBroker. Subscribing assigns every partition of
    the topics on the next `poll`; `reassign()` simulates a rebalance (incremental: only the
    difference is revoked/assigned, inside the next `poll`, as with Kafka)."""

    def __init__(self, broker: FakeBroker, group: str) -> None:
        self.broker = broker
        self.group = group
        self._assigned: set[TopicPartition] = set()
        self._positions: dict[TopicPartition, int] = {}
        self._paused: set[TopicPartition] = set()
        self._pending: set[TopicPartition] | None = None
        self._on_assign: PartitionsCallback | None = None
        self._on_revoke: PartitionsCallback | None = None
        self._rr = 0
        self.closed = False

    def subscribe(
        self,
        topics: Iterable[str],
        *,
        on_assign: PartitionsCallback | None = None,
        on_revoke: PartitionsCallback | None = None,
    ) -> None:
        self._on_assign, self._on_revoke = on_assign, on_revoke
        self._pending = {
            TopicPartition(t, p) for t in topics for p in range(self.broker.partitions(t))
        }

    def reassign(self, partitions: Iterable[TopicPartition]) -> None:
        self._pending = set(partitions)

    def _rebalance(self) -> None:
        if self._pending is None:
            return
        new, self._pending = self._pending, None
        revoked = sorted(self._assigned - new)
        added = sorted(new - self._assigned)
        if revoked:
            if self._on_revoke:
                self._on_revoke(revoked)
            for tp in revoked:
                self._assigned.discard(tp)
                self._positions.pop(tp, None)
                self._paused.discard(tp)
        if added:
            for tp in added:
                committed = self.broker.committed(self.group, tp.topic).get(tp.partition, 0)
                self._positions[tp] = committed
                self._assigned.add(tp)
            if self._on_assign:
                self._on_assign(added)

    def poll(self, timeout: float = 0.0) -> Message | None:
        self._rebalance()
        live = [tp for tp in sorted(self._assigned) if tp not in self._paused]
        for i in range(len(live)):
            tp = live[(self._rr + i) % len(live)]
            msg = self.broker.fetch(tp.topic, tp.partition, self._positions[tp])
            if msg is not None:
                self._rr = (self._rr + i + 1) % len(live)
                self._positions[tp] = msg.offset + 1
                return msg
        if timeout > 0:
            time.sleep(timeout)
        return None

    def commit(self, offsets: Mapping[TopicPartition, int]) -> None:
        if offsets:
            self.broker.commit(self.group, offsets)

    def pause(self, partitions: Iterable[TopicPartition]) -> None:
        self._paused.update(tp for tp in partitions if tp in self._assigned)

    def resume(self, partitions: Iterable[TopicPartition]) -> None:
        self._paused.difference_update(partitions)

    def paused(self) -> set[TopicPartition]:
        return set(self._paused)

    def assignment(self) -> set[TopicPartition]:
        return set(self._assigned)

    def position(self, tp: TopicPartition) -> int | None:
        return self._positions.get(tp)

    def close(self) -> None:
        self.closed = True
