"""The durable ticket job (demo/durable) fed from a Kafka topic: one process runs a KafkaSource
on a FakeBroker plus an agentline worker. Kill it at any point; a fresh process resumes from the
committed offsets and the redelivered messages map back onto their existing runs.

Env vars (same contract as the other demo workers, plus the broker):
    LEDGER_DB        path to the agentline SQLite ledger (required)
    FAKES_URL        base URL of demo.fakes.server (required)
    BROKER_DB        path to the FakeBroker SQLite file (required)
    KAFKA_TOPIC      topic to consume (default "tickets")
    KAFKA_GROUP      consumer group (default "agentline")
    CONCURRENCY      worker slots (default 4)
    LEASE_TTL        seconds (default 1.0)
    MAX_IN_FLIGHT    uncommitted messages per partition before it pauses (default 4)
    COMMIT_INTERVAL  seconds between offset commits (default 0.2)
    EXIT_WHEN_DONE   "1" -> exit once every partition is committed up to its end offset
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from agentline.sources import FakeBroker, KafkaSource
from demo.durable.worker import build_app


def caught_up(broker: FakeBroker, group: str, topic: str) -> bool:
    committed = broker.committed(group, topic)
    return all(committed.get(p, 0) >= end for p, end in broker.end_offsets(topic).items())


async def run() -> None:
    app = build_app(os.environ["LEDGER_DB"], os.environ["FAKES_URL"])
    broker = FakeBroker(os.environ["BROKER_DB"])
    topic = os.environ.get("KAFKA_TOPIC", "tickets")
    group = os.environ.get("KAFKA_GROUP", "agentline")
    source = KafkaSource(
        app,
        broker.consumer(group),
        pipeline="ticket",
        topics=[topic],
        max_in_flight_per_partition=int(os.environ.get("MAX_IN_FLIGHT", "4")),
        commit_interval=float(os.environ.get("COMMIT_INTERVAL", "0.2")),
        poll_timeout=0.05,
    )
    worker = app.worker(
        concurrency=int(os.environ.get("CONCURRENCY", "4")),
        lease_ttl=float(os.environ.get("LEASE_TTL", "1.0")),
        poll_interval=0.05,
    )
    stop = asyncio.Event()
    tasks = [asyncio.create_task(worker.run(stop)), asyncio.create_task(source.run(stop))]
    try:
        if os.environ.get("EXIT_WHEN_DONE") == "1":
            while not await asyncio.to_thread(caught_up, broker, group, topic):
                await asyncio.sleep(0.1)
            stop.set()
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
