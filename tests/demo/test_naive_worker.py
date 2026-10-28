"""Deterministic crash tests: prove the naive worker duplicates effects, without randomness."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from demo.fakes.server import get_json
from demo.naive.queue import JobQueue

from .conftest import REPO_ROOT


def run_worker(tmp_path: Path, fakes_url: str, **env: str) -> int:
    full_env = {
        **os.environ,
        "QUEUE_DB": str(tmp_path / "q.db"),
        "FAKES_URL": fakes_url,
        "CONCURRENCY": "1",
        "VISIBILITY_TIMEOUT": "0.2",
        "EXIT_WHEN_DONE": "1",
        **env,
    }
    proc = subprocess.run(
        [sys.executable, "-m", "demo.naive.worker"],
        cwd=REPO_ROOT,
        env=full_env,
        timeout=30,
    )
    return proc.returncode


def test_no_crash_one_pr_one_llm_call(tmp_path: Path, fakes_url: str) -> None:
    JobQueue(tmp_path / "q.db").enqueue("T-1")
    assert run_worker(tmp_path, fakes_url) == 0
    stats = get_json(f"{fakes_url}/stats")
    assert stats["prs_by_ticket"] == {"T-1": 1}
    assert stats["llm_calls_by_job"] == {"T-1": 1}


def test_crash_after_pr_opens_a_duplicate_pr(tmp_path: Path, fakes_url: str) -> None:
    JobQueue(tmp_path / "q.db").enqueue("T-1")
    crash = {"CRASH_AT": "after_pr", "CRASH_MARKER": str(tmp_path / "crashed")}
    assert run_worker(tmp_path, fakes_url, **crash) == 137
    assert run_worker(tmp_path, fakes_url, **crash) == 0
    stats = get_json(f"{fakes_url}/stats")
    assert stats["prs_by_ticket"] == {"T-1": 2}  # the bug Phase 1 must fix
    assert stats["llm_calls_by_job"] == {"T-1": 2}


def test_crash_after_llm_repeats_the_paid_call(tmp_path: Path, fakes_url: str) -> None:
    JobQueue(tmp_path / "q.db").enqueue("T-1")
    crash = {"CRASH_AT": "after_llm", "CRASH_MARKER": str(tmp_path / "crashed")}
    assert run_worker(tmp_path, fakes_url, **crash) == 137
    assert run_worker(tmp_path, fakes_url, **crash) == 0
    stats = get_json(f"{fakes_url}/stats")
    assert stats["prs_by_ticket"] == {"T-1": 1}
    assert stats["llm_calls_by_job"] == {"T-1": 2}
