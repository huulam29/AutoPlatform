from __future__ import annotations

import time
from pathlib import Path

from demo.naive.queue import JobQueue


def test_claim_hides_job_until_ack(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "q.db", visibility_timeout=60)
    q.enqueue("T-1")
    job = q.claim()
    assert job is not None and job.ticket == "T-1" and job.attempts == 1
    assert q.claim() is None
    q.ack(job.id)
    assert q.all_done()


def test_unacked_job_is_redelivered_after_timeout(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "q.db", visibility_timeout=0.05)
    q.enqueue("T-1")
    first = q.claim()
    assert first is not None
    time.sleep(0.1)
    second = q.claim()
    assert second is not None
    assert second.id == first.id and second.attempts == 2


def test_acked_job_is_never_redelivered(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "q.db", visibility_timeout=0.01)
    q.enqueue("T-1")
    job = q.claim()
    assert job is not None
    q.ack(job.id)
    time.sleep(0.05)
    assert q.claim() is None


def test_two_connections_never_claim_the_same_job(tmp_path: Path) -> None:
    a = JobQueue(tmp_path / "q.db", visibility_timeout=60)
    b = JobQueue(tmp_path / "q.db", visibility_timeout=60)
    for i in range(20):
        a.enqueue(f"T-{i}")
    claimed = []
    for _ in range(10):
        for q in (a, b):
            job = q.claim()
            assert job is not None
            claimed.append(job.id)
    assert len(claimed) == len(set(claimed)) == 20


def test_all_done_is_false_for_empty_queue(tmp_path: Path) -> None:
    assert not JobQueue(tmp_path / "q.db").all_done()
