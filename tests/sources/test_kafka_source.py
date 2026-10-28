from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agentline import App, SqliteLedger, StepContext
from agentline.sources import (
    FakeBroker,
    FakeConsumer,
    KafkaSource,
    TopicPartition,
    dedupe_key,
)

TOPIC = "t"
P0 = TopicPartition(TOPIC, 0)
P1 = TopicPartition(TOPIC, 1)


def make_app(tmp_path: Path) -> App:
    app = App(SqliteLedger(tmp_path / "ledger.db"), artifact_root=tmp_path / "artifacts")
    flow = app.flow("job")

    @flow.step("work")
    async def work(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"seen": inputs["payload"]["n"]}

    return app


@pytest.fixture
def broker(tmp_path: Path) -> FakeBroker:
    b = FakeBroker(tmp_path / "broker.db")
    b.create_topic(TOPIC, 2)
    return b


def produce(broker: FakeBroker, n: int, partition: int = 0) -> None:
    for i in range(n):
        broker.produce(TOPIC, json.dumps({"n": i}), partition=partition)


def source(app: App, consumer: FakeConsumer, **kw: Any) -> KafkaSource:
    kw.setdefault("commit_interval", 0)
    kw.setdefault("refresh_interval", 0)
    return KafkaSource(app, consumer, pipeline="job", topics=[TOPIC], poll_timeout=0, **kw)


def drain(src: KafkaSource, polls: int = 20) -> int:
    got = 0
    for _ in range(polls):
        if src.poll_once() is not None:
            got += 1
    return got


def run_id(app: App, partition: int, offset: int) -> str:
    with app.ledger.tx() as tx:
        run = tx.get_run_by_dedupe(dedupe_key(TOPIC, partition, offset))
    assert run is not None
    return run.id


def set_status(app: App, partition: int, offset: int, status: str) -> None:
    rid = run_id(app, partition, offset)
    with app.ledger.tx() as tx:
        tx.update_run(rid, status=status)


def committed(broker: FakeBroker, group: str = "g") -> dict[int, int]:
    return broker.committed(group, TOPIC)


def all_runs(app: App) -> list[Any]:
    with app.ledger.tx() as tx:
        return tx.list_runs(limit=1000)


# -- run identity -------------------------------------------------------------------------------
def test_one_run_per_message_keyed_by_coordinates(tmp_path: Path, broker: FakeBroker) -> None:
    app = make_app(tmp_path)
    produce(broker, 3)
    src = source(app, broker.consumer("g"))
    assert drain(src) == 3
    runs = all_runs(app)
    assert sorted(r.dedupe_key for r in runs) == [f"kafka:{TOPIC}:0:{i}" for i in range(3)]
    assert sorted(r.payload["n"] for r in runs) == [0, 1, 2]


# -- contiguous commits -------------------------------------------------------------------------
def test_out_of_order_completion_commits_only_the_contiguous_prefix(
    tmp_path: Path, broker: FakeBroker
) -> None:
    app = make_app(tmp_path)
    produce(broker, 3)
    src = source(app, broker.consumer("g"))
    drain(src)
    assert committed(broker) == {}

    set_status(app, 0, 1, "done")
    set_status(app, 0, 2, "done")
    src.maintain()
    assert committed(broker) == {}  # offset 0 is still open

    set_status(app, 0, 0, "done")
    src.maintain()
    assert committed(broker) == {0: 3}


def test_waiting_run_holds_the_offset_until_it_is_dead(tmp_path: Path, broker: FakeBroker) -> None:
    app = make_app(tmp_path)
    produce(broker, 3)
    src = source(app, broker.consumer("g"), pause_on=())
    drain(src)
    set_status(app, 0, 0, "done")
    set_status(app, 0, 1, "waiting")
    set_status(app, 0, 2, "done")
    src.maintain()
    assert committed(broker) == {0: 1}

    set_status(app, 0, 1, "needs-review")
    src.maintain()
    assert committed(broker) == {0: 1}

    set_status(app, 0, 1, "dead")
    src.maintain()
    assert committed(broker) == {0: 3}


def test_cancelled_counts_as_terminal(tmp_path: Path, broker: FakeBroker) -> None:
    app = make_app(tmp_path)
    produce(broker, 1)
    src = source(app, broker.consumer("g"))
    drain(src)
    asyncio.run(app.cancel(run_id(app, 0, 0), actor="t"))
    src.maintain()
    assert committed(broker) == {0: 1}


def test_commit_interval_batches_commits(tmp_path: Path, broker: FakeBroker) -> None:
    app = make_app(tmp_path)
    produce(broker, 2)
    now = [0.0]
    src = source(app, broker.consumer("g"), commit_interval=10, clock=lambda: now[0])
    drain(src)
    set_status(app, 0, 0, "done")
    set_status(app, 0, 1, "done")
    src.maintain()
    assert committed(broker) == {}  # not due yet (first commit happened at t=0)
    now[0] = 11
    src.maintain()
    assert committed(broker) == {0: 2}


# -- pause / resume -----------------------------------------------------------------------------
def test_full_window_pauses_the_partition_and_draining_resumes_it(
    tmp_path: Path, broker: FakeBroker
) -> None:
    app = make_app(tmp_path)
    produce(broker, 4, partition=0)
    produce(broker, 1, partition=1)
    consumer = broker.consumer("g")
    src = source(app, consumer, max_in_flight_per_partition=2)
    assert drain(src) == 3  # two from p0, then it pauses; p1 keeps flowing
    assert consumer.paused() == {P0}
    assert len(all_runs(app)) == 3

    set_status(app, 0, 0, "done")
    src.maintain()
    assert consumer.paused() == set()
    assert drain(src) == 1  # room for one more
    assert consumer.paused() == {P0}

    for off in (1, 2):
        set_status(app, 0, off, "done")
    src.maintain()
    assert drain(src) == 1
    assert committed(broker)[0] == 3


def test_long_running_head_pauses_until_it_finishes(tmp_path: Path, broker: FakeBroker) -> None:
    app = make_app(tmp_path)
    produce(broker, 1)
    consumer = broker.consumer("g")
    src = source(app, consumer, max_in_flight_per_partition=100)
    drain(src)
    set_status(app, 0, 0, "waiting")
    src.maintain()
    assert consumer.paused() == {P0}

    produce(broker, 1)
    assert drain(src) == 0  # paused: the new message isn't fetched
    set_status(app, 0, 0, "done")
    src.maintain()
    assert consumer.paused() == set()
    assert drain(src) == 1


# -- rebalance ----------------------------------------------------------------------------------
def test_rebalance_commits_and_forgets_revoked_partitions(
    tmp_path: Path, broker: FakeBroker
) -> None:
    app = make_app(tmp_path)
    produce(broker, 2, partition=0)
    produce(broker, 2, partition=1)
    consumer = broker.consumer("g")
    src = source(app, consumer, commit_interval=1e9)  # commits only when forced/revoked
    drain(src)
    set_status(app, 1, 0, "done")
    first_runs = {(p, o): run_id(app, p, o) for p in (0, 1) for o in (0, 1)}

    consumer.reassign([P0])
    src.poll_once()
    assert set(src.parts) == {P0}
    assert committed(broker) == {1: 1}  # what was final on p1 got committed on revoke

    # p1 comes back: resumes from offset 1; the redelivered message maps onto its old run
    consumer.reassign([P0, P1])
    assert drain(src) == 1
    assert consumer.position(P1) == 2
    assert src.counters["redelivered"] == 1
    assert len(all_runs(app)) == 4
    assert {(p, o): run_id(app, p, o) for p in (0, 1) for o in (0, 1)} == first_runs


# -- crash and redelivery -----------------------------------------------------------------------
def test_redelivery_after_a_crash_creates_no_second_run(tmp_path: Path, broker: FakeBroker) -> None:
    app = make_app(tmp_path)
    produce(broker, 3)
    src = source(app, broker.consumer("g"))
    drain(src)
    set_status(app, 0, 0, "done")
    src.maintain()
    assert committed(broker) == {0: 1}
    before = {o: run_id(app, 0, o) for o in range(3)}
    # crash: the consumer and source vanish without another commit

    app2 = make_app(tmp_path)  # a restarted process: same ledger, same broker file
    broker2 = FakeBroker(tmp_path / "broker.db")
    src2 = source(app2, broker2.consumer("g"))
    assert drain(src2) == 2  # offsets 1 and 2 again
    assert (src2.counters["created"], src2.counters["redelivered"]) == (0, 2)
    assert len(all_runs(app)) == 3
    assert {o: run_id(app, 0, o) for o in range(3)} == before


# -- poison -------------------------------------------------------------------------------------
def test_poison_message_becomes_a_dead_run_and_its_offset_commits(
    tmp_path: Path, broker: FakeBroker
) -> None:
    app = make_app(tmp_path)
    broker.produce(TOPIC, b"\xff not json", partition=0, key="k1")
    produce(broker, 1)
    src = source(app, broker.consumer("g"))
    drain(src)
    poison = app.get_run(run_id(app, 0, 0))
    assert poison is not None
    assert poison.status == "dead"
    assert "poison message" in (poison.error or "")
    assert poison.payload["kafka"] == {"topic": TOPIC, "partition": 0, "offset": 0}
    assert app.executions(poison.id) == []
    assert [e.action for e in app.audit_log(poison.id)] == ["poison"]
    assert src.counters["poison"] == 1

    set_status(app, 0, 1, "done")
    src.maintain()
    assert committed(broker) == {0: 2}

    # a redelivered poison message maps onto the same dead run
    src2 = source(app, FakeBroker(tmp_path / "broker.db").consumer("other-group"))
    drain(src2)
    assert src2.counters["poison"] == 0
    assert len(all_runs(app)) == 2


# -- end to end ---------------------------------------------------------------------------------
def test_source_and_worker_drain_the_topic(tmp_path: Path, broker: FakeBroker) -> None:
    app = make_app(tmp_path)
    produce(broker, 5, partition=0)
    produce(broker, 4, partition=1)
    src = KafkaSource(
        app,
        broker.consumer("g"),
        pipeline="job",
        topics=[TOPIC],
        max_in_flight_per_partition=2,
        commit_interval=0.05,
        refresh_interval=0.02,
        poll_timeout=0.01,
    )

    async def main() -> None:
        stop = asyncio.Event()
        worker = app.worker(concurrency=2, lease_ttl=5, poll_interval=0.01)
        tasks = [asyncio.create_task(worker.run(stop)), asyncio.create_task(src.run(stop))]
        for _ in range(500):
            if committed(broker) == {0: 5, 1: 4}:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.gather(*tasks)

    asyncio.run(main())
    assert committed(broker) == {0: 5, 1: 4}
    assert {r.status for r in all_runs(app)} == {"done"}
    assert len(all_runs(app)) == 9
