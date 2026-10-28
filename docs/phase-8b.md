# Phase 8b: Kafka source

Branch `phase-8b`. A `KafkaSource` turns every message on a Kafka topic into exactly one
agentline run. Offsets are committed by hand, and only past runs that have finished, so a crash
at any point loses nothing and a redelivered message never starts a second run.

## Pieces

| File | What |
|---|---|
| `src/agentline/sources/consumer.py` | `Consumer` protocol, `Message`, `TopicPartition`; `ConfluentConsumer` (real adapter); `FakeBroker` / `FakeConsumer` (SQLite-backed stand-in) |
| `src/agentline/sources/kafka.py` | `KafkaSource`, `dedupe_key()`, `json_payload()` |
| `demo/kafka/worker.py` | one process: KafkaSource on a FakeBroker + an agentline worker for the `demo/durable` ticket job |
| `demo/chaos.py` | `KafkaTarget` (`--target kafka`) |
| `tests/sources/`, `tests/demo/test_kafka_chaos.py` | tests |

The core stays stdlib-only. `confluent-kafka` is the optional extra `kafka`
(`pip install 'agentline[kafka]'` or `uv sync --extra kafka`). It is imported only when a
`ConfluentConsumer` is built, and a missing install raises an `ImportError` that names the extra.

### Consumer protocol

```python
class Consumer(Protocol):
    def subscribe(self, topics, *, on_assign=None, on_revoke=None) -> None: ...
    def poll(self, timeout: float) -> Message | None: ...  # rebalance callbacks fire in here
    def commit(self, offsets: Mapping[TopicPartition, int]) -> None: ...  # synchronous
    def pause(self, partitions) -> None: ...
    def resume(self, partitions) -> None: ...
    def assignment(self) -> set[TopicPartition]: ...
    def close(self) -> None: ...
```

Offsets follow Kafka's convention: a committed offset is the *next* offset to read.

- **`ConfluentConsumer(config)`** forces `enable.auto.commit=false` and
  `enable.auto.offset.store=false`, and defaults `auto.offset.reset=earliest`. It maps
  `on_lost` to `on_revoke`. `_PARTITION_EOF` is swallowed, non-fatal errors are logged, and
  fatal errors raise `KafkaConsumerError`.
- **`FakeBroker(path)`** keeps topics, messages (with a per-message delivery counter) and
  committed group offsets in one SQLite file (WAL, busy timeout). Producer, consumer and the
  chaos harness can live in different processes. Keyed messages go to `crc32(key) % partitions`.
  A `FakeConsumer` keeps its positions in memory, so after a `kill -9` a fresh consumer starts
  from the committed offset and everything after it is redelivered. `reassign()` simulates an
  incremental rebalance, with callbacks delivered inside the next `poll`.

## KafkaSource

```python
source = KafkaSource(
    app,
    consumer,
    pipeline="ticket-to-eod",  # flow the runs are submitted to
    topics=["jira-events"],
    payload=json_payload,  # Message -> run payload; raising marks the message poison
    max_in_flight_per_partition=16,
    commit_interval=1.0,
)
await source.run(stop)  # or source.poll_once() in a loop you own
```

For each message:

1. `payload(msg)`. If it raises, the message is **poison** (see below).
2. `app.submit_sync(pipeline, payload, dedupe_key=f"kafka:{topic}:{partition}:{offset}")`.
   On a redelivery this returns the existing run (`created=False`), and the payload computed
   the second time is ignored.
3. The run id and status are appended to the partition's **window**: the messages delivered
   but not yet committable, kept in delivery order.

Housekeeping (`maintain()`, after every poll) re-reads the status of the open runs in the
windows, every `refresh_interval` (0.2 s) in one ledger transaction. It then pops the window
from the front while the head run is terminal, pauses or resumes, and commits every
`commit_interval`.

### Commit semantics

- **Contiguous only.** The commit point of a partition is `offset + 1` of the last message in
  an unbroken run of terminal heads. A finished run behind an open one waits. Terminal means
  `done`, `dead` or `cancelled`, the same set as `TERMINAL_RUN`. `waiting`, `needs-review`,
  `failed` (backing off), `pending` and `running` all hold the offset.
- **Contiguous in delivery order, not offset + 1.** Gaps in offsets (compaction, transaction
  markers) don't stall the commit.
- **Manual and synchronous.** `consumer.commit({tp: offset})` runs only when the point moved,
  at most once per `commit_interval`, on revoke, and once more when `run()` exits.
- **Guarantees.** Delivery from Kafka is **at least once**. Run creation is **exactly once** per
  `(topic, partition, offset)`, enforced by the ledger's unique `dedupe_key` inside the submit
  transaction. A committed offset means every earlier message has a terminal run, so nothing
  committed is ever lost.

### Pause policy

A partition is **paused** when either of these holds:

- its window holds `max_in_flight_per_partition` messages, or
- the run at the head of its window has a status in `pause_on`. The default is `waiting` and
  `needs-review`: the run is waiting on a gate or a human, so the offset can't move for a while.

It is **resumed** as soon as neither holds. Paused partitions keep their position. The source
keeps calling `poll`, so other partitions keep flowing and the group membership stays alive
(`max.poll.interval.ms` is never hit by a long gate). The window size also bounds how much is
redelivered after a crash: at most `max_in_flight` messages per partition, all of them no-ops.

Set `pause_on=()` to keep consuming behind a waiting run until the window is full.

### Rebalance

`on_revoke(partitions)` refreshes those partitions, commits whatever is final, and forgets their
windows. If the commit fails because the partition is already lost, the error is logged and
ignored. The open runs keep going in the ledger, since any worker can run them. The next owner
redelivers from the committed offset, and each message maps back onto the same run through
`dedupe_key`. `on_assign` starts fresh windows. A message for an offset already in the window,
or below the commit point, is ignored as a duplicate delivery.

### Poison messages

When `payload(msg)` raises, a run is inserted directly with status `dead`. It has the same
`dedupe_key`, no executions, `error = "poison message: <Type>: <msg>"`, and a redacted payload
`{"kafka": {topic, partition, offset}, "key", "value_preview"}` (first 2 KiB). An audit row
(`actor kafka-source`, `action poison`) is written in the same transaction. Because the run is
terminal, the offset commits and the partition doesn't stall. A redelivered poison message maps
back onto the same dead run.

## Residual windows

1. **Crash before a commit.** Up to `max_in_flight` messages per partition are redelivered.
   Every one is a no-op submit. The chaos run's `redeliveries` counts these.
2. **Zombie owner after a rebalance.** For a short while two consumers can both think they own
   a partition. Both commit only terminal prefixes, so a commit can move *backwards* (an older,
   smaller point lands last). That costs extra redelivery, never loss, and no extra runs.
3. **Offset reuse.** If a topic is deleted and recreated, or a partition is truncated and its
   offsets restart, a new message at an old `(topic, partition, offset)` maps onto the old run
   and is silently skipped. See proposal 2.
4. **Ledger errors.** An exception from `submit` (for example the database is down) propagates
   out of `run()`. The process should exit and restart. Nothing after that message was committed.
5. **librdkafka prefetch.** After `pause`, messages already fetched for the partition can still
   arrive. They are accepted, so a window can briefly exceed its limit.
6. **Status refresh cost.** One `get_run` per open run every `refresh_interval`. That is fine
   for hundreds of open runs; a batched status query belongs to the ledger (8a).
7. **Poison runs can't be retried.** `agentline runs retry` re-arms dead *executions*, and a
   poison run has none. Fix the producer and re-publish.

## Running against a real Kafka

There is no broker in this environment, so the adapter is tested against a stand-in module
(`tests/sources/test_consumer.py`). It was also checked against the real `confluent-kafka`
2.16.0: construct, subscribe with all three callbacks, and poll an unreachable broker.

```python
import asyncio

from agentline.sources import ConfluentConsumer, KafkaSource


async def main(app) -> None:
    consumer = ConfluentConsumer(
        {"bootstrap.servers": "kafka:9092", "group.id": "agentline", "client.id": "agentline-1"}
    )
    source = KafkaSource(app, consumer, pipeline="ticket-to-eod", topics=["jira-events"])
    stop = asyncio.Event()
    try:
        await asyncio.gather(app.worker().run(stop), source.run(stop))
    finally:
        consumer.close()
```

Any extra librdkafka settings (SASL, TLS, `session.timeout.ms`) pass straight through `config`,
except auto-commit, which is always off. Run one source per process. Several processes in the
same group share the partitions in the usual Kafka way.

## Tests

- `tests/sources/test_kafka_source.py`:
  - one run per message, with the exact key format;
  - out-of-order completion commits only the contiguous prefix;
  - `waiting` and then `needs-review` hold the offset, and `dead` releases it;
  - `cancelled` is terminal;
  - `commit_interval` batching;
  - a full window pauses one partition while another keeps flowing, and draining resumes it;
  - a waiting head pauses, and finishing resumes;
  - rebalance commits on revoke, forgets the partition, and re-assignment maps the redelivered
    message onto its old run;
  - a crash plus a fresh process redelivers with no second run;
  - a poison message becomes a dead run with an audit row, its offset commits, and redelivery
    creates no new run;
  - end to end with `run()` and a real worker.
- `tests/sources/test_consumer.py`:
  - FakeBroker keyed partitioning and offsets;
  - resume from the committed offset in a new connection, with delivery counts;
  - pause keeps the position;
  - rebalance callbacks fire inside `poll`;
  - the confluent adapter's config, callbacks, commit, pause and poll error mapping, against a
    stand-in module;
  - the missing-extra error.
- `tests/demo/test_kafka_chaos.py`:
  - the report flags lost messages and uncommitted offsets;
  - the demo worker drains a topic and exits (`EXIT_WHEN_DONE=1`);
  - a slow-marked 12-job chaos run must come out clean.

## Chaos acceptance

`uv run python -m demo.chaos --target kafka --jobs 50 --seed N --expect-clean`. The setup is
4 partitions, a window of 4, a 0.2 s commit interval, 4 worker slots and a 2 s lease. The
harness produces 50 keyed messages, then SIGKILLs the combined source + worker process at
random. A ticket counts as done only when its run is `done` *and* its offset is committed, so
the harness runs until committed offsets reach the end offsets. The run is clean only if, on top
of the usual PR/LLM checks, no message lacks a run, every ticket has exactly one run, and
committed offsets equal end offsets for every partition.

| seed | kills | elapsed | PRs (dup) | LLM calls (extra / bound) | deliveries (redelivered) | lost | runs/ticket ≠ 1 | committed = end | CLEAN |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 18 | 18.0 s | 50 (0) | 70 (20 / 72) | 207 (157) | 0 | 0 | yes | yes |
| 2 | 9 | 10.4 s | 50 (0) | 57 (7 / 36) | 153 (103) | 0 | 0 | yes | yes |
| 3 | 11 | 10.4 s | 50 (0) | 64 (14 / 44) | 186 (136) | 0 | 0 | yes | yes |
| 4 | 15 | 13.1 s | 50 (0) | 67 (17 / 60) | 215 (165) | 0 | 0 | yes | yes |
| 5 | 7 | 9.0 s | 50 (0) | 56 (6 / 28) | 135 (85) | 0 | 0 | yes | yes |
| 6 | 10 | 10.5 s | 50 (0) | 62 (12 / 40) | 170 (120) | 0 | 0 | yes | yes |

Redeliveries are high on purpose: every kill drops up to 4 uncommitted messages per partition,
and each one comes back as a no-op submit. The 50 runs per seed show they cost nothing.

## Decisions (proposals for the user)

1. **The window counts uncommitted messages, not running ones.** "In flight" for
   `max_in_flight_per_partition` is every delivered message not yet committable. That includes
   finished runs stuck behind an open head. One number then bounds memory, concurrency and
   crash redelivery.
2. **The dedupe key is exactly `kafka:{topic}:{partition}:{offset}`**, as briefed. Recreating a
   topic reuses keys (residual 3). Proposal: an optional `key_prefix` (a cluster id or topic
   incarnation) once someone needs it.
3. **Pause on a `waiting` / `needs-review` head by default.** This favours per-partition
   ordering and a bounded backlog over throughput. `pause_on=()` turns it off.
4. **`failed` (backing off) holds the offset.** Only `done`, `dead` and `cancelled` release it.
   A run that later dies releases it then.
5. **Poison means the payload function raised.** The source writes a dead run directly, with no
   executions, plus an audit row. It doesn't go through the scheduler, because there is nothing
   to execute. Alternative: route poison to a dead-letter topic as well. Not done, because there
   is no producer in the Consumer protocol.
6. **The rebalance is incremental in the fake.** The source copes with both eager and
   cooperative assignment, because on revoke it only touches the partitions named.
7. **Not wired into the CLI or `agentline.toml` yet.** The CLI is 8c's area. Proposal: a
   `[sources.kafka]` table (`bootstrap`, `group`, `topics`, `pipeline`, `payload =
   "module:fn"`, limits) and `agentline source kafka` to run it next to a worker.
8. **The chaos harness has one shared hook.** `run_chaos` now ANDs a target's
   `extra_report()["clean"]` into the overall verdict (two lines), so target-specific invariants
   fail `--expect-clean`.
