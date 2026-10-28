from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from demo.chaos import ChaosConfig, KafkaTarget, run_chaos

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_kafka_report_flags_lost_messages_and_uncommitted_offsets(tmp_path: Path) -> None:
    target = KafkaTarget(tmp_path, concurrency=1, visibility_timeout=1.0)
    target.submit(["T-1", "T-2"])
    report = target.extra_report()  # nothing consumed: no runs, nothing committed
    assert sorted(report["kafka"]["lost_messages"]) == sorted(
        f"{p}:{o}" for p, o in target.messages
    )
    assert report["kafka"]["tickets_without_exactly_one_run"] == {"T-1": 0, "T-2": 0}
    assert report["clean"] is False
    assert target.done_tickets() == set()


def test_kafka_worker_drains_the_topic_and_exits(tmp_path: Path, fakes_url: str) -> None:
    target = KafkaTarget(tmp_path, concurrency=2, visibility_timeout=1.0)
    target.submit([f"T-{i}" for i in range(6)])
    env = {**os.environ, **target.worker_env(fakes_url), "EXIT_WHEN_DONE": "1"}
    subprocess.run(
        [sys.executable, "-m", "demo.kafka.worker"], cwd=REPO_ROOT, env=env, check=True, timeout=60
    )
    assert target.done_tickets() == {f"T-{i}" for i in range(6)}
    assert target.extra_report()["clean"] is True


@pytest.mark.slow
def test_small_kafka_chaos_run_is_clean(tmp_path: Path) -> None:
    cfg = ChaosConfig(
        target="kafka",
        jobs=12,
        seed=5,
        concurrency=2,
        visibility_timeout=0.5,
        kill_min=0.1,
        kill_max=0.4,
        max_kills=5,
        timeout=60,
        out=tmp_path,
    )
    r = run_chaos(cfg)
    assert r["kills"] == 5
    assert r["clean"], r
    kafka = r["engine"]["kafka"]
    assert kafka["lost_messages"] == []
    assert kafka["committed_offsets"] == kafka["end_offsets"]
    assert r["prs"]["total"] == 12
