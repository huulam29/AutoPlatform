from __future__ import annotations

from pathlib import Path

import pytest

from demo.chaos import ChaosConfig, build_report, main, run_chaos


def test_report_flags_duplicates_and_missing() -> None:
    cfg = ChaosConfig(jobs=3, concurrency=2)
    stats = {
        "prs": 3,
        "prs_by_ticket": {"T-1": 2, "T-2": 1},
        "llm_calls": 5,
        "llm_calls_by_job": {"T-1": 3, "T-2": 1, "T-3": 1},
    }
    r = build_report(cfg, ["T-1", "T-2", "T-3"], {"T-1", "T-2", "T-3"}, stats, 1, 0, 1.0)
    assert r["prs"]["duplicates"] == {"T-1": 2}
    assert r["prs"]["done_without_pr"] == ["T-3"]
    assert r["llm"]["extra"] == 2
    assert r["llm"]["repeat_bound"] == 2
    assert not r["clean"]


def test_report_clean_when_one_effect_per_ticket() -> None:
    cfg = ChaosConfig(jobs=2, concurrency=1)
    stats = {
        "prs": 2,
        "prs_by_ticket": {"T-1": 1, "T-2": 1},
        "llm_calls": 3,
        "llm_calls_by_job": {"T-1": 2, "T-2": 1},
    }
    r = build_report(cfg, ["T-1", "T-2"], {"T-1", "T-2"}, stats, 1, 0, 1.0)
    assert r["clean"]


@pytest.mark.slow
def test_small_chaos_run_finishes_every_job(tmp_path: Path) -> None:
    cfg = ChaosConfig(
        jobs=10,
        seed=7,
        concurrency=2,
        visibility_timeout=0.5,
        kill_min=0.1,
        kill_max=0.4,
        max_kills=5,
        timeout=60,
        out=tmp_path,
    )
    r = run_chaos(cfg)
    assert r["runs"]["not_done"] == []
    assert r["kills"] == 5
    assert r["prs"]["total"] >= 10
    assert r["llm"]["total"] >= 10
    assert (Path(r["workdir"]) / "report.json").exists()


@pytest.mark.slow
def test_expect_clean_fails_when_naive_worker_duplicates(tmp_path: Path) -> None:
    # Slow PR responses + frequent kills make a lost response (and so a duplicate) near certain.
    argv = [
        "--jobs", "6", "--seed", "3", "--concurrency", "2", "--visibility-timeout", "0.5",
        "--kill-min", "0.1", "--kill-max", "0.3", "--max-kills", "15",
        "--llm-latency", "0.01,0.02", "--pr-latency", "0.3,0.5",
        "--out", str(tmp_path), "--expect-clean",
    ]  # fmt: skip
    assert main(argv) == 1
