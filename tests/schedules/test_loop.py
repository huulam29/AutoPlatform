"""The schedule loop: due fire times become runs, catch-up policies, no double fires."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentline.app import App
from agentline.builtins import default_registry
from agentline.ledger import SqliteLedger
from agentline.pipeline import Pipeline, build_pipeline, parse_spec
from agentline.schedules import ScheduleLoop, dedupe_key, next_fire_times, run_schedules, scheduled

SPEC = """\
version: 1
name: {name}
trigger:
  type: schedule
  schedule: "{cron}"
{extra}
nodes:
  tests:
    type: action
    uses: shell.run
    with: {{ cmd: "true" }}
edges: []
"""


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


def pipeline(name: str = "nightly", cron: str = "0 2 * * *", **trigger: str) -> Pipeline:
    extra = "".join(f"  {k}: {v}\n" for k, v in trigger.items())
    spec = parse_spec(SPEC.format(name=name, cron=cron, extra=extra.rstrip("\n")))
    return build_pipeline(spec, default_registry())


def make_app(db: Path, *pipelines: Pipeline) -> App:
    app = App(SqliteLedger(db))
    for p in pipelines:
        app.register_pipeline(p)
    return app


def fired(app: App, name: str = "nightly") -> list[str]:
    with app.ledger.tx() as tx:
        runs = tx.list_runs(None, 10_000)
    prefix = f"schedule:{name}:"
    return sorted(
        r.dedupe_key[len(prefix) :] for r in runs if (r.dedupe_key or "").startswith(prefix)
    )


def loop(app: App, clock: Clock, *pipelines: Pipeline) -> ScheduleLoop:
    return ScheduleLoop(app, scheduled(pipelines), clock)


def test_fires_when_due_with_payload_and_dedupe_key(tmp_path: Path) -> None:
    p = pipeline()
    app = make_app(tmp_path / "db", p)
    clock = Clock(utc(2026, 10, 19, 1, 59, 50))
    sl = loop(app, clock, p)
    assert sl.tick() == utc(2026, 10, 19, 2)
    assert fired(app) == []
    clock.advance(seconds=15)
    sl.tick()
    assert fired(app) == ["2026-10-19T02:00:00Z"]
    with app.ledger.tx() as tx:
        run = tx.get_run_by_dedupe("schedule:nightly:2026-10-19T02:00:00Z")
    assert run is not None and run.flow == "nightly"
    assert run.payload == {"scheduled_for": "2026-10-19T02:00:00Z", "pipeline": "nightly"}
    clock.advance(minutes=5)
    sl.tick()
    assert fired(app) == ["2026-10-19T02:00:00Z"]  # nothing new


def test_never_fired_pipeline_starts_fresh(tmp_path: Path) -> None:
    p = pipeline(catchup="all")
    app = make_app(tmp_path / "db", p)
    loop(app, Clock(utc(2026, 10, 19, 15)), p).tick()  # 02:00 was 13 h ago, before we existed
    assert fired(app) == []


def _downtime(tmp_path: Path, policy: str) -> list[str]:
    """Fired at 02:00 on the 15th, then down until the 19th at 12:00."""
    p = pipeline(catchup=policy)
    app = make_app(tmp_path / f"{policy}.db", p)
    clock = Clock(utc(2026, 10, 15, 2, 0, 5))
    loop(app, clock, p).tick()
    assert fired(app) == ["2026-10-15T02:00:00Z"]
    clock.now = utc(2026, 10, 19, 12)
    loop(app, clock, p).tick()  # a restarted process
    return fired(app)


def test_catchup_latest_is_default(tmp_path: Path) -> None:
    assert pipeline().spec.trigger.catchup == "latest"
    assert _downtime(tmp_path, "latest") == ["2026-10-15T02:00:00Z", "2026-10-19T02:00:00Z"]


def test_catchup_all(tmp_path: Path) -> None:
    assert _downtime(tmp_path, "all") == [f"2026-10-{d}T02:00:00Z" for d in (15, 16, 17, 18, 19)]


def test_catchup_none(tmp_path: Path) -> None:
    assert _downtime(tmp_path, "none") == ["2026-10-15T02:00:00Z"]


def test_catchup_while_running_after_a_stall(tmp_path: Path) -> None:
    """A suspended process (laptop sleep) wakes up with several fire times past."""
    p = pipeline("every5", "*/5 * * * *")
    app = make_app(tmp_path / "db", p)
    clock = Clock(utc(2026, 10, 19, 10, 0, 1))
    sl = loop(app, clock, p)
    sl.tick()
    assert fired(app, "every5") == ["2026-10-19T10:00:00Z"]
    clock.advance(minutes=22)  # 10:05 .. 10:20 missed
    sl.tick()
    assert fired(app, "every5") == ["2026-10-19T10:00:00Z", "2026-10-19T10:20:00Z"]


def test_two_loops_on_one_ledger_never_double_fire(tmp_path: Path) -> None:
    p = pipeline("every5", "*/5 * * * *", catchup="all")
    db = tmp_path / "db"
    app1, app2 = make_app(db, p), make_app(db, p)
    clock = Clock(utc(2026, 10, 19, 10, 0, 1))
    l1, l2 = loop(app1, clock, p), loop(app2, clock, p)
    for _ in range(12):
        l1.tick()
        l2.tick()
        clock.advance(minutes=1, seconds=7)
    got = fired(app1, "every5")
    assert got == [f"2026-10-19T10:{m:02d}:00Z" for m in (0, 5, 10)]
    created = [c for _, _, c in l1.submitted + l2.submitted]
    assert created.count(True) == 3 and created.count(False) == 2  # l2 found 10:00 at start


def test_concurrent_async_loops_never_double_fire(tmp_path: Path) -> None:
    p = pipeline("every-min", "* * * * *")
    db = tmp_path / "db"
    apps = [make_app(db, p) for _ in range(3)]
    clock = Clock(utc(2026, 10, 19, 10, 0, 30))

    async def main() -> None:
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(run_schedules(a, [p], stop, clock, max_sleep=0.01)) for a in apps
        ]
        await asyncio.sleep(0.1)
        for _ in range(4):
            clock.advance(minutes=1)
            await asyncio.sleep(0.1)
        stop.set()
        await asyncio.gather(*tasks)

    asyncio.run(main())
    assert fired(apps[0], "every-min") == [f"2026-10-19T10:{m:02d}:00Z" for m in range(5)]


def test_run_schedules_returns_without_schedule_pipelines(tmp_path: Path) -> None:
    spec = parse_spec(
        "version: 1\nname: m\nnodes:\n  a: { type: action, uses: shell.run, with: { cmd: x } }\n"
    )
    p = build_pipeline(spec, default_registry())
    app = make_app(tmp_path / "db", p)
    asyncio.run(asyncio.wait_for(run_schedules(app, [p], asyncio.Event()), 1))


def test_timezone_and_next_fire_times() -> None:
    p = pipeline(cron="0 9 * * mon-fri", timezone="Europe/Berlin")
    rows = next_fire_times([p], utc(2026, 10, 23, 12), 2)  # a Friday
    assert rows == [
        {
            "pipeline": "nightly",
            "schedule": "0 9 * * mon-fri",
            "timezone": "Europe/Berlin",
            "catchup": "latest",
            "next": ["2026-10-26T08:00:00Z", "2026-10-27T08:00:00Z"],  # CET after Oct 25
            "next_local": ["2026-10-26T09:00:00+01:00", "2026-10-27T09:00:00+01:00"],
        }
    ]


def test_dedupe_key_format() -> None:
    assert dedupe_key("x", utc(2026, 1, 2, 3, 4)) == "schedule:x:2026-01-02T03:04:00Z"


def test_submit_failure_is_retried_next_tick(tmp_path: Path) -> None:
    p = pipeline()
    app = make_app(tmp_path / "db", p)
    clock = Clock(utc(2026, 10, 19, 2, 0, 5))
    sl = loop(app, clock, p)
    real = app.submit_sync
    calls = {"n": 0}

    def flaky(*a: object, **kw: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real(*a, **kw)  # type: ignore[arg-type]

    app.submit_sync = flaky  # type: ignore[method-assign]
    assert sl.tick() == clock.now + timedelta(seconds=1)
    assert fired(app) == []
    clock.advance(seconds=1)
    sl.tick()
    assert fired(app) == ["2026-10-19T02:00:00Z"]


@pytest.mark.parametrize("policy", ["latest", "all", "none"])
def test_policy_parsed(policy: str) -> None:
    assert pipeline(catchup=policy).spec.trigger.catchup == policy
