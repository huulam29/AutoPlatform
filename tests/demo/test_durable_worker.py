"""The same crash points that break the naive worker, against the durable engine."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from demo.chaos import ChaosConfig, run_chaos
from demo.durable.worker import build_app
from demo.fakes.server import get_json

from .conftest import REPO_ROOT


def run_worker(tmp_path: Path, fakes_url: str, **env: str) -> int:
    full_env = {
        **os.environ,
        "LEDGER_DB": str(tmp_path / "ledger.db"),
        "FAKES_URL": fakes_url,
        "CONCURRENCY": "1",
        "LEASE_TTL": "0.3",
        "EXIT_WHEN_DONE": "1",
        **env,
    }
    proc = subprocess.run(
        [sys.executable, "-m", "demo.durable.worker"], cwd=REPO_ROOT, env=full_env, timeout=30
    )
    return proc.returncode


def submit(tmp_path: Path, fakes_url: str) -> str:
    app = build_app(str(tmp_path / "ledger.db"), fakes_url)
    return app.submit_sync("ticket", {"ticket": "T-1"}, dedupe_key="T-1").run_id


def run_status(tmp_path: Path, fakes_url: str, run_id: str) -> str:
    run = build_app(str(tmp_path / "ledger.db"), fakes_url).get_run(run_id)
    assert run is not None
    return run.status


@pytest.mark.parametrize(
    ("point", "llm_calls"),
    [("after_llm", 2), ("after_pr_post", 1), ("after_pr", 1)],
)
def test_crash_never_duplicates_the_pr(
    tmp_path: Path, fakes_url: str, point: str, llm_calls: int
) -> None:
    run_id = submit(tmp_path, fakes_url)
    crash = {"CRASH_AT": point, "CRASH_MARKER": str(tmp_path / "crashed")}
    assert run_worker(tmp_path, fakes_url, **crash) == 137
    assert run_worker(tmp_path, fakes_url, **crash) == 0  # waits out the lease, then resumes
    stats = get_json(f"{fakes_url}/stats")
    assert stats["prs_by_ticket"] == {"T-1": 1}
    assert stats["llm_calls_by_job"] == {"T-1": llm_calls}
    assert run_status(tmp_path, fakes_url, run_id) == "done"


@pytest.mark.slow
def test_small_durable_chaos_run_is_clean(tmp_path: Path) -> None:
    cfg = ChaosConfig(
        target="durable",
        jobs=8,
        seed=3,
        concurrency=2,
        visibility_timeout=0.5,
        kill_min=0.1,
        kill_max=0.3,
        max_kills=10,
        llm_latency="0.01,0.02",
        pr_latency="0.3,0.5",  # same settings that make the naive worker duplicate
        timeout=90,
        out=tmp_path,
    )
    r = run_chaos(cfg)
    assert r["clean"], r
    assert r["prs"]["total"] == 8


@pytest.mark.slow
def test_small_pipeline_chaos_run_is_clean(tmp_path: Path) -> None:
    cfg = ChaosConfig(
        target="pipeline",
        jobs=6,
        seed=5,
        concurrency=2,
        visibility_timeout=0.5,
        kill_min=0.2,
        kill_max=0.5,
        max_kills=8,
        llm_latency="0.01,0.05",
        pr_latency="0.2,0.4",
        timeout=120,
        out=tmp_path,
    )
    r = run_chaos(cfg)
    assert r["clean"], r
    assert r["prs"]["total"] == 6 and r["deploys"]["total"] == 6
    assert r["engine"]["reviews_sent"] == 12 and r["engine"]["approvals_sent"] == 6
