"""Chaos harness: submit N tickets, SIGKILL the worker at random points, report the damage.

    uv run python -m demo.chaos --jobs 50 --seed 1
    uv run python -m demo.chaos --target naive --expect-clean   # exit 1 on any duplicate
    uv run python -m demo.chaos --target durable --ledger-url postgresql://u:p@host/db

The harness owns two processes: the fake services (never killed) and the worker (killed with
SIGKILL at random intervals, then restarted). After `--max-kills` kills the worker is left
alone to finish. The report counts PRs per ticket and LLM calls per job from the fakes' own
records, so it doesn't trust anything the worker says about itself.

A *target* says how to submit work, how to start the worker and how to tell when a ticket is
done. Phase 0 has `naive`; later phases add the durable engine as another target.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from demo.fakes.server import get_json

REPO_ROOT = Path(__file__).resolve().parents[1]


class Target(Protocol):
    def submit(self, tickets: Sequence[str]) -> None: ...
    def worker_cmd(self) -> list[str]: ...
    def worker_env(self, fakes_url: str) -> dict[str, str]: ...
    def done_tickets(self) -> set[str]: ...


class NaiveTarget:
    def __init__(self, workdir: Path, concurrency: int, visibility_timeout: float) -> None:
        from demo.naive.queue import JobQueue

        self.db = workdir / "queue.db"
        self.concurrency = concurrency
        self.visibility_timeout = visibility_timeout
        self.queue = JobQueue(self.db, visibility_timeout=visibility_timeout)

    def submit(self, tickets: Sequence[str]) -> None:
        for t in tickets:
            self.queue.enqueue(t)

    def worker_cmd(self) -> list[str]:
        return [sys.executable, "-m", "demo.naive.worker"]

    def worker_env(self, fakes_url: str) -> dict[str, str]:
        return {
            "QUEUE_DB": str(self.db),
            "FAKES_URL": fakes_url,
            "CONCURRENCY": str(self.concurrency),
            "VISIBILITY_TIMEOUT": str(self.visibility_timeout),
        }

    def done_tickets(self) -> set[str]:
        return {str(j["ticket"]) for j in self.queue.jobs() if j["status"] == "done"}


class DurableTarget:
    """agentline engine. `visibility_timeout` is used as the lease TTL."""

    def __init__(
        self,
        workdir: Path,
        concurrency: int,
        visibility_timeout: float,
        ledger_url: str | None = None,
    ) -> None:
        from demo.durable.worker import build_app

        self.db: Path | str = ledger_url or workdir / "ledger.db"
        self.concurrency = concurrency
        self.lease_ttl = visibility_timeout
        self._build = build_app
        self._app: Any = None

    def _get(self, fakes_url: str = "") -> Any:
        if self._app is None:
            self._app = self._build(str(self.db), fakes_url)
        return self._app

    def submit(self, tickets: Sequence[str]) -> None:
        app = self._get()
        for _ in range(2):  # every webhook delivered twice: dedupe_key must absorb it
            for t in tickets:
                app.submit_sync("ticket", {"ticket": t}, dedupe_key=f"jira:{t}:assigned")

    def worker_cmd(self) -> list[str]:
        return [sys.executable, "-m", "demo.durable.worker"]

    def worker_env(self, fakes_url: str) -> dict[str, str]:
        return {
            "LEDGER_DB": str(self.db),
            "FAKES_URL": fakes_url,
            "CONCURRENCY": str(self.concurrency),
            "LEASE_TTL": str(self.lease_ttl),
        }

    def done_tickets(self) -> set[str]:
        with self._get().ledger.tx() as tx:
            runs = tx.list_runs(limit=100_000)
        return {r.payload["ticket"] for r in runs if r.status == "done"}

    def extra_report(self) -> dict[str, Any]:
        with self._get().ledger.tx() as tx:
            runs = tx.list_runs(limit=100_000)
            reclaims = sum(x.reclaims for r in runs for x in tx.list_executions(r.id))
        by_status: dict[str, int] = {}
        for r in runs:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        return {"runs_total": len(runs), "runs_by_status": by_status, "lease_reclaims": reclaims}


class PipelineTarget(DurableTarget):
    """examples/ticket-to-eod. The harness plays reviewer and approver: every run gets
    changes_requested on its first review, approved on the second, then gate approval."""

    llm_per_ticket = 3  # plan#1, implement#1, implement#2
    expects_deploys = True

    def __init__(
        self,
        workdir: Path,
        concurrency: int,
        visibility_timeout: float,
        ledger_url: str | None = None,
    ) -> None:
        super().__init__(workdir, concurrency, visibility_timeout, ledger_url)
        from demo.pipeline.worker import build_app

        self._build = build_app
        self.reviews_sent = 0
        self.approvals_sent = 0

    def submit(self, tickets: Sequence[str]) -> None:
        app = self._get()
        for _ in range(2):
            for t in tickets:
                app.submit_sync("ticket-to-eod", {"issue_key": t}, dedupe_key=f"jira:{t}:assigned")

    def worker_cmd(self) -> list[str]:
        return [sys.executable, "-m", "demo.pipeline.worker"]

    def done_tickets(self) -> set[str]:
        with self._get().ledger.tx() as tx:
            runs = tx.list_runs(limit=100_000)
        return {r.payload["issue_key"] for r in runs if r.status == "done"}

    def tick(self) -> None:
        import asyncio as aio

        app = self._get()
        with app.ledger.tx() as tx:
            waiting = [
                x
                for r in tx.list_runs(status="waiting", limit=100_000)
                for x in tx.list_executions(r.id)
                if x.status == "waiting"
            ]
        for ex in waiting:
            if ex.detail.get("kind") == "gate":
                aio.run(app.approve(ex.run_id, ex.node_id, actor="chaos-approver"))
                self.approvals_sent += 1
            elif (ex.correlation_key or "").startswith("github:pr:"):
                state = "changes_requested" if ex.iteration == 1 else "approved"
                comments = ["handle the empty password case"] if ex.iteration == 1 else []
                aio.run(
                    app.deliver_event(
                        ex.correlation_key or "", {"state": state, "comments": comments}
                    )
                )
                self.reviews_sent += 1

    def extra_report(self) -> dict[str, Any]:
        out = super().extra_report()
        out.update(reviews_sent=self.reviews_sent, approvals_sent=self.approvals_sent)
        return out


class KafkaTarget(DurableTarget):
    """The durable ticket job fed from a FakeBroker topic (several partitions) by KafkaSource.
    A ticket counts as done only once its run is done *and* its offset is committed, so the
    harness keeps going until committed offsets reach the end offsets."""

    topic = "tickets"
    group = "agentline"
    partitions = 4
    max_in_flight = 4

    def __init__(self, workdir: Path, concurrency: int, visibility_timeout: float) -> None:
        super().__init__(workdir, concurrency, visibility_timeout)
        from agentline.sources import FakeBroker

        self.broker_db = workdir / "broker.db"
        self.broker = FakeBroker(self.broker_db)
        self.broker.create_topic(self.topic, self.partitions)
        self.messages: dict[tuple[int, int], str] = {}  # (partition, offset) -> ticket

    def submit(self, tickets: Sequence[str]) -> None:
        for t in tickets:
            coords = self.broker.produce(self.topic, json.dumps({"ticket": t}), key=t)
            self.messages[coords] = t

    def worker_cmd(self) -> list[str]:
        return [sys.executable, "-m", "demo.kafka.worker"]

    def worker_env(self, fakes_url: str) -> dict[str, str]:
        return {
            **super().worker_env(fakes_url),
            "BROKER_DB": str(self.broker_db),
            "KAFKA_TOPIC": self.topic,
            "KAFKA_GROUP": self.group,
            "MAX_IN_FLIGHT": str(self.max_in_flight),
            "COMMIT_INTERVAL": "0.2",
        }

    def done_tickets(self) -> set[str]:
        committed = self.broker.committed(self.group, self.topic)
        acked = {t for (p, o), t in self.messages.items() if o < committed.get(p, 0)}
        return super().done_tickets() & acked

    def extra_report(self) -> dict[str, Any]:
        out = super().extra_report()
        from agentline.sources import dedupe_key

        with self._get().ledger.tx() as tx:
            runs = tx.list_runs(limit=100_000)
            lost = sorted(
                f"{p}:{o}"
                for p, o in self.messages
                if tx.get_run_by_dedupe(dedupe_key(self.topic, p, o)) is None
            )
        runs_per_ticket: dict[str, int] = {}
        for r in runs:
            t = r.payload.get("ticket") if isinstance(r.payload, dict) else None
            runs_per_ticket[str(t)] = runs_per_ticket.get(str(t), 0) + 1
        not_one = {
            t: runs_per_ticket.get(t, 0)
            for t in sorted(self.messages.values())
            if runs_per_ticket.get(t, 0) != 1
        }
        ends = self.broker.end_offsets(self.topic)
        committed = self.broker.committed(self.group, self.topic)
        deliveries = sum(m["deliveries"] for m in self.broker.messages(self.topic))
        out["kafka"] = {
            "messages": len(self.messages),
            "deliveries": deliveries,
            "redeliveries": deliveries - len(self.messages),
            "lost_messages": lost,
            "tickets_without_exactly_one_run": not_one,
            "end_offsets": ends,
            "committed_offsets": committed,
        }
        out["clean"] = (
            not lost and not not_one and all(committed.get(p, 0) == end for p, end in ends.items())
        )
        return out


TARGETS: dict[str, Any] = {
    "naive": NaiveTarget,
    "durable": DurableTarget,
    "pipeline": PipelineTarget,
    "kafka": KafkaTarget,
}


@dataclass
class ChaosConfig:
    target: str = "naive"
    jobs: int = 50
    seed: int = 1
    concurrency: int = 4
    visibility_timeout: float = 2.0
    kill_min: float = 0.3
    kill_max: float = 1.5
    max_kills: int = 20
    timeout: float = 180.0
    llm_latency: str = "0.05,0.3"
    pr_latency: str = "0.05,0.2"
    worker_cmd: str | None = None
    out: Path = REPO_ROOT / "chaos-out"
    ledger_url: str | None = None  # durable/pipeline: e.g. postgresql://...; default SQLite


def run_ledger_url(url: str | None, workdir: Path) -> str | None:
    """A Postgres URL gets a fresh schema per chaos run (`?schema=chaos_<run>`), so runs never
    see each other's tickets. Other URLs are used as given."""
    from agentline.ledger import is_postgres_url, split_schema

    if not url or not is_postgres_url(url) or split_schema(url)[1]:
        return url
    schema = "chaos_" + "".join(c if c.isalnum() else "_" for c in workdir.name.lower())
    return f"{url}{'&' if '?' in url else '?'}schema={schema}"


def start_fakes(workdir: Path, cfg: ChaosConfig) -> tuple[subprocess.Popen[bytes], str]:
    port_file = workdir / "fakes.port"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "demo.fakes.server",
            "--db",
            str(workdir / "fakes.db"),
            "--port-file",
            str(port_file),
            "--llm-latency",
            cfg.llm_latency,
            "--pr-latency",
            cfg.pr_latency,
        ],
        cwd=REPO_ROOT,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if port_file.exists() and port_file.read_text():
            url = f"http://127.0.0.1:{port_file.read_text()}"
            try:
                get_json(f"{url}/health", timeout=1)
                return proc, url
            except OSError:
                pass
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("fake services did not start")


def run_chaos(cfg: ChaosConfig) -> dict[str, Any]:
    rng = random.Random(cfg.seed)
    workdir = cfg.out / f"{cfg.target}-seed{cfg.seed}-{int(time.time())}"
    workdir.mkdir(parents=True)
    if cfg.ledger_url and cfg.target == "naive":
        raise SystemExit("--ledger-url works with the durable and pipeline targets only")
    extra = {"ledger_url": run_ledger_url(cfg.ledger_url, workdir)} if cfg.ledger_url else {}
    target: Target = TARGETS[cfg.target](workdir, cfg.concurrency, cfg.visibility_timeout, **extra)
    tickets = [f"T-{i:03d}" for i in range(1, cfg.jobs + 1)]

    fakes, fakes_url = start_fakes(workdir, cfg)
    cmd = shlex.split(cfg.worker_cmd) if cfg.worker_cmd else target.worker_cmd()
    env = {**os.environ, **target.worker_env(fakes_url)}

    def spawn() -> subprocess.Popen[bytes]:
        return subprocess.Popen(cmd, cwd=REPO_ROOT, env=env)

    started = time.monotonic()
    kills = 0
    unexpected_exits = 0
    target.submit(tickets)
    worker = spawn()
    try:
        while len(target.done_tickets()) < len(tickets):
            if time.monotonic() - started > cfg.timeout:
                break
            wait_until = time.monotonic() + rng.uniform(cfg.kill_min, cfg.kill_max)
            while time.monotonic() < wait_until and worker.poll() is None:
                if hasattr(target, "tick"):
                    target.tick()
                time.sleep(0.05)
            if len(target.done_tickets()) == len(tickets):
                break
            if worker.poll() is not None:
                unexpected_exits += 1
                worker = spawn()
            elif kills < cfg.max_kills:
                worker.send_signal(signal.SIGKILL)
                worker.wait()
                kills += 1
                worker = spawn()
        elapsed = time.monotonic() - started
        stats = get_json(f"{fakes_url}/stats")
        done = target.done_tickets()
    finally:
        for proc in (worker, fakes):
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    report = build_report(
        cfg,
        tickets,
        done,
        stats,
        kills,
        unexpected_exits,
        elapsed,
        llm_per_ticket=getattr(target, "llm_per_ticket", 1),
        expects_deploys=getattr(target, "expects_deploys", False),
    )
    if hasattr(target, "extra_report"):
        report["engine"] = target.extra_report()
        if report["engine"].get("clean") is False:  # target-specific invariants (kafka)
            report["clean"] = False
    report["workdir"] = str(workdir)
    (workdir / "report.json").write_text(json.dumps(report, indent=2))
    return report


def build_report(
    cfg: ChaosConfig,
    tickets: Sequence[str],
    done: set[str],
    stats: dict[str, Any],
    kills: int,
    unexpected_exits: int,
    elapsed: float,
    *,
    llm_per_ticket: int = 1,
    expects_deploys: bool = False,
) -> dict[str, Any]:
    prs_by_ticket: dict[str, int] = stats["prs_by_ticket"]
    llm_by_job: dict[str, int] = stats["llm_calls_by_job"]
    duplicate_prs = {t: n for t, n in sorted(prs_by_ticket.items()) if n > 1}
    missing_prs = sorted(t for t in done if prs_by_ticket.get(t, 0) == 0)
    not_done = sorted(set(tickets) - done)
    repeated_llm = {j: n for j, n in sorted(llm_by_job.items()) if n > 1}
    llm_extra = sum(n - 1 for n in llm_by_job.values() if n > 1)
    deploys_by_ticket: dict[str, int] = stats.get("deploys_by_ticket", {})
    dup_deploys = {t: n for t, n in sorted(deploys_by_ticket.items()) if n > 1}
    missing_deploys = sorted(t for t in done if deploys_by_ticket.get(t, 0) == 0)
    # A kill interrupts at most `concurrency` in-flight executions, each repeating once.
    llm_repeat_bound = kills * cfg.concurrency
    return {
        "target": cfg.target,
        "seed": cfg.seed,
        "jobs": len(tickets),
        "concurrency": cfg.concurrency,
        "kills": kills,
        "unexpected_exits": unexpected_exits,
        "elapsed_s": round(elapsed, 2),
        "runs": {"done": len(done), "not_done": not_done},
        "prs": {
            "total": stats["prs"],
            "expected": len(tickets),
            "tickets_with_duplicates": len(duplicate_prs),
            "extra": sum(n - 1 for n in duplicate_prs.values()),
            "duplicates": duplicate_prs,
            "done_without_pr": missing_prs,
        },
        "llm": {
            "total": stats["llm_calls"],
            "expected": len(tickets) * llm_per_ticket,
            "extra": llm_extra,
            "repeat_bound": llm_repeat_bound,
            "repeated": repeated_llm,
        },
        "deploys": {
            "total": stats.get("deploys", 0),
            "tickets_with_duplicates": len(dup_deploys),
            "duplicates": dup_deploys,
            "done_without_deploy": missing_deploys if expects_deploys else [],
        },
        "clean": not duplicate_prs
        and not missing_prs
        and not not_done
        and not dup_deploys
        and not (expects_deploys and missing_deploys)
        and llm_extra <= llm_repeat_bound,
    }


def format_report(r: dict[str, Any]) -> str:
    prs, llm = r["prs"], r["llm"]
    lines = [
        f"chaos report: target={r['target']} seed={r['seed']} jobs={r['jobs']} "
        f"concurrency={r['concurrency']}",
        f"  kills (SIGKILL)          {r['kills']}   unexpected exits {r['unexpected_exits']}",
        f"  elapsed                  {r['elapsed_s']} s",
        f"  runs done / not done     {r['runs']['done']} / {len(r['runs']['not_done'])}",
        f"  PRs total / expected     {prs['total']} / {prs['expected']}",
        f"  tickets with dup PRs     {prs['tickets_with_duplicates']} (extra PRs: {prs['extra']})",
        f"  done without a PR        {len(prs['done_without_pr'])}",
        f"  LLM calls / expected     {llm['total']} / {llm['expected']} (extra: {llm['extra']}, "
        f"bound kills x concurrency: {llm['repeat_bound']})",
        f"  deploys / dup tickets    {r['deploys']['total']} / "
        f"{r['deploys']['tickets_with_duplicates']}",
        f"  CLEAN                    {'yes' if r['clean'] else 'NO'}",
    ]
    if "engine" in r:
        lines.append(f"  engine                   {r['engine']}")
    if prs["duplicates"]:
        dups = ", ".join(f"{t}x{n}" for t, n in prs["duplicates"].items())
        lines.append(f"  duplicate PRs: {dups}")
    lines.append(f"  report: {r['workdir']}/report.json")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> tuple[ChaosConfig, bool]:
    d = ChaosConfig()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--target", choices=sorted(TARGETS), default=d.target)
    p.add_argument("--jobs", type=int, default=d.jobs)
    p.add_argument("--seed", type=int, default=d.seed)
    p.add_argument("--concurrency", type=int, default=d.concurrency)
    p.add_argument("--visibility-timeout", type=float, default=d.visibility_timeout)
    p.add_argument("--kill-min", type=float, default=d.kill_min)
    p.add_argument("--kill-max", type=float, default=d.kill_max)
    p.add_argument("--max-kills", type=int, default=d.max_kills)
    p.add_argument("--timeout", type=float, default=d.timeout)
    p.add_argument("--llm-latency", default=d.llm_latency)
    p.add_argument("--pr-latency", default=d.pr_latency)
    p.add_argument("--worker-cmd", help="override the target's worker command")
    p.add_argument("--out", type=Path, default=d.out)
    p.add_argument("--expect-clean", action="store_true", help="exit 1 unless the run is clean")
    p.add_argument(
        "--ledger-url",
        help="durable/pipeline ledger: a path, sqlite:///path or postgresql://... (a Postgres"
        " URL gets a fresh schema per run); default: SQLite in the run's workdir",
    )
    a = p.parse_args(argv)
    cfg = ChaosConfig(
        target=a.target,
        jobs=a.jobs,
        seed=a.seed,
        concurrency=a.concurrency,
        visibility_timeout=a.visibility_timeout,
        kill_min=a.kill_min,
        kill_max=a.kill_max,
        max_kills=a.max_kills,
        timeout=a.timeout,
        llm_latency=a.llm_latency,
        pr_latency=a.pr_latency,
        worker_cmd=a.worker_cmd,
        out=a.out,
        ledger_url=a.ledger_url,
    )
    return cfg, a.expect_clean


def main(argv: Sequence[str] | None = None) -> int:
    cfg, expect_clean = parse_args(argv)
    report = run_chaos(cfg)
    print(format_report(report))
    return 1 if expect_clean and not report["clean"] else 0


if __name__ == "__main__":
    sys.exit(main())
