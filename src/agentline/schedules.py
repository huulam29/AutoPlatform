"""Scheduled triggers: a 5-field cron parser and the loop that submits runs when they are due.

Cron (`Cron.parse`): `minute hour day-of-month month day-of-week`, each `*`, a number, a
range `a-b`, a list `a,b,c`, and steps `*/n`, `a-b/n`, `a/n`. Month and weekday names
(`jan`, `mon`) work; weekday 0 and 7 are Sunday. As in classic cron, when both day-of-month
and day-of-week are restricted, a day matching *either* fires. Aliases: `@yearly`
(`@annually`), `@monthly`, `@weekly`, `@daily` (`@midnight`), `@hourly`.

Times are evaluated in UTC or in a configured IANA timezone (`zoneinfo`). Daylight saving:
- a wall time that doesn't exist (spring forward) fires once, shifted forward by the gap
  (02:30 on the switch day fires at 03:30 local);
- a wall time that happens twice (fall back) fires once, on the first pass, unless the
  hour field is `*`: then it fires on both passes, so hourly schedules keep firing hourly.
Fire times are identified by their UTC instant, so two wall times that map to the same
instant fire once.

The loop (`run_schedules`) submits each fire time with
`dedupe_key = schedule:<pipeline>:<UTC time>`, so restarts and several processes on one
ledger never fire twice. On start it walks back from now (at most `LOOKBACK` fire times)
until it finds a fire time that already has a run; the ones after it were missed while
nothing was running. `catchup` decides which of those fire: `latest` (default, only the
most recent), `all` (every one, at most `LOOKBACK`), or `none` (only ones still within the
grace period). A pipeline that has never fired starts fresh: nothing is caught up.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("agentline.schedules")

CATCHUP_POLICIES = ("latest", "all", "none")
DEFAULT_CATCHUP = "latest"
LOOKBACK = 1000  # fire times examined (and caught up, for `all`) after downtime
DEFAULT_GRACE = 60.0  # seconds a fire time counts as on time for `catchup: none`
MAX_SLEEP = 30.0
_SEARCH_DAYS = 366 * 12  # covers Feb 29 on a given weekday and other sparse schedules
_DST_MARGIN = timedelta(hours=3)  # larger than any real DST shift

ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
FIELDS = (  # name, low, high, names
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day-of-month", 1, 31, {}),
    ("month", 1, 12, MONTHS),
    ("day-of-week", 0, 7, DAYS),
)


class CronError(ValueError):
    pass


def _value(text: str, lo: int, hi: int, names: dict[str, int], name: str) -> int:
    t = text.lower()
    if t in names:
        return names[t]
    if not t.isdigit():
        raise CronError(f"{name}: {text!r} is not a number")
    v = int(t)
    if not lo <= v <= hi:
        raise CronError(f"{name}: {v} is out of range {lo}-{hi}")
    return v


def _field(text: str, lo: int, hi: int, names: dict[str, int], name: str) -> frozenset[int]:
    out: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"{name}: empty list item in {text!r}")
        rng, _, step_s = part.partition("/")
        step = 1
        if step_s:
            if not step_s.isdigit() or int(step_s) == 0:
                raise CronError(f"{name}: bad step {step_s!r}")
            step = int(step_s)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a_s, _, b_s = rng.partition("-")
            a, b = _value(a_s, lo, hi, names, name), _value(b_s, lo, hi, names, name)
            if a > b:
                raise CronError(f"{name}: range {rng!r} goes backwards")
        else:
            a = _value(rng, lo, hi, names, name)
            b = hi if step_s else a  # `a/n` means a, a+n, ... up to the maximum
        out.update(range(a, b + 1, step))
    return frozenset(out)


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    dom_any: bool
    dow_any: bool
    hour_any: bool

    @classmethod
    def parse(cls, expr: str) -> Cron:
        if not isinstance(expr, str) or not expr.strip():
            raise CronError("empty cron expression")
        text = expr.strip()
        if text.startswith("@"):
            if text.lower() not in ALIASES:
                raise CronError(f"unknown alias {text!r} (known: {', '.join(sorted(ALIASES))})")
            text = ALIASES[text.lower()]
        parts = text.split()
        if len(parts) != 5:
            raise CronError(
                f"expected 5 fields (minute hour day-of-month month day-of-week), got {len(parts)}"
            )
        sets = [
            _field(p, lo, hi, names, n) for p, (n, lo, hi, names) in zip(parts, FIELDS, strict=True)
        ]
        weekdays = frozenset(d % 7 for d in sets[4])
        days, months = sets[2], sets[3]
        if parts[2] != "*" and parts[4] == "*":
            longest = max(calendar.monthrange(2024, m)[1] for m in months)
            if min(days) > longest:
                raise CronError(f"day-of-month {min(days)} never occurs in the given months")
        return cls(
            expr=expr.strip(),
            minutes=sets[0],
            hours=sets[1],
            days=days,
            months=months,
            weekdays=weekdays,
            dom_any=parts[2].startswith("*"),
            dow_any=parts[4].startswith("*"),
            hour_any=parts[1] == "*",
        )

    # -- wall-clock (naive) search ---------------------------------------------------------
    def day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.weekdays
        if self.dom_any or self.dow_any:
            return dom and dow
        return dom or dow

    def next_wall(self, w: datetime) -> datetime:
        """The first matching naive wall time strictly after `w` (minute precision)."""
        start = w.replace(second=0, microsecond=0) + timedelta(minutes=1)
        d = start.date()
        hours, minutes = sorted(self.hours), sorted(self.minutes)
        for _ in range(_SEARCH_DAYS):
            if self.day_matches(d):
                first_day = d == start.date()
                for h in hours:
                    if first_day and h < start.hour:
                        continue
                    for m in minutes:
                        if first_day and h == start.hour and m < start.minute:
                            continue
                        return datetime(d.year, d.month, d.day, h, m)
            d += timedelta(days=1)
        raise CronError(f"{self.expr!r} never fires")

    def prev_wall(self, w: datetime) -> datetime:
        """The last matching naive wall time strictly before `w` (minute precision)."""
        floor = w.replace(second=0, microsecond=0)
        start = floor - timedelta(minutes=1) if floor == w else floor
        d = start.date()
        hours, minutes = sorted(self.hours, reverse=True), sorted(self.minutes, reverse=True)
        for _ in range(_SEARCH_DAYS):
            if self.day_matches(d):
                first_day = d == start.date()
                for h in hours:
                    if first_day and h > start.hour:
                        continue
                    for m in minutes:
                        if first_day and h == start.hour and m > start.minute:
                            continue
                        return datetime(d.year, d.month, d.day, h, m)
            d -= timedelta(days=1)
        raise CronError(f"{self.expr!r} never fires")


@dataclass(frozen=True)
class Schedule:
    """A cron expression evaluated in a timezone. All public times are aware UTC."""

    cron: Cron
    tz: tzinfo = UTC

    @classmethod
    def parse(cls, expr: str, timezone: str | None = None) -> Schedule:
        return cls(Cron.parse(expr), load_tz(timezone))

    def _naive(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz).replace(tzinfo=None)

    def _instants(self, w: datetime) -> list[datetime]:
        """UTC instants at which wall time `w` fires (see the module doc for DST rules)."""
        a = w.replace(tzinfo=self.tz, fold=0).astimezone(UTC)
        if self.tz is UTC:
            return [a]
        b = w.replace(tzinfo=self.tz, fold=1).astimezone(UTC)
        if a == b or self._naive(a) != w:  # unambiguous, or nonexistent (shifted forward)
            return [a]
        return [a, b] if self.cron.hour_any else [a]

    def _stable(self, *points: datetime) -> bool:
        """No UTC offset change within the margin around any of the points."""
        if self.tz is UTC:
            return True
        for p in points:
            lo = (p - _DST_MARGIN).astimezone(self.tz).utcoffset()
            hi = (p + _DST_MARGIN).astimezone(self.tz).utcoffset()
            if lo != hi:
                return False
        return True

    def next_after(self, dt: datetime) -> datetime:
        """The first fire time strictly after `dt` (aware), as an aware UTC datetime."""
        dt = _utc(dt)
        w = self.cron.next_wall(self._naive(dt))
        later = [i for i in self._instants(w) if i > dt]
        if later and self._stable(dt, min(later)):
            return min(later)
        # near a DST change wall order and instant order can differ: search a window
        w = self._naive(dt) - _DST_MARGIN
        best: datetime | None = None
        best_wall = w
        while best is None or w <= best_wall + _DST_MARGIN:
            w = self.cron.next_wall(w)
            for i in self._instants(w):
                if i > dt and (best is None or i < best):
                    best, best_wall = i, w
        return best

    def prev_before(self, dt: datetime) -> datetime:
        """The last fire time strictly before `dt`, as an aware UTC datetime."""
        dt = _utc(dt)
        w = self.cron.prev_wall(self._naive(dt))
        earlier = [i for i in self._instants(w) if i < dt]
        if earlier and self._stable(dt, max(earlier)):
            return max(earlier)
        w = self._naive(dt) + _DST_MARGIN
        best: datetime | None = None
        best_wall = w
        while best is None or w >= best_wall - _DST_MARGIN:
            w = self.cron.prev_wall(w)
            for i in self._instants(w):
                if i < dt and (best is None or i > best):
                    best, best_wall = i, w
        return best

    def upcoming(self, dt: datetime, n: int) -> list[datetime]:
        out: list[datetime] = []
        for _ in range(n):
            dt = self.next_after(dt)
            out.append(dt)
        return out


def load_tz(name: str | None) -> tzinfo:
    if not name or name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise CronError(f"unknown timezone {name!r}") from e


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("schedule times must be timezone-aware")
    return dt.astimezone(UTC)


def fire_iso(t: datetime) -> str:
    return _utc(t).strftime("%Y-%m-%dT%H:%M:%SZ")


def dedupe_key(pipeline: str, t: datetime) -> str:
    return f"schedule:{pipeline}:{fire_iso(t)}"


# -- pipelines with schedule triggers ---------------------------------------------------------
@dataclass
class ScheduledPipeline:
    name: str
    schedule: Schedule
    catchup: str = DEFAULT_CATCHUP
    cursor: datetime | None = None  # fire times at or before this are handled


def scheduled(pipelines: Iterable[Any]) -> list[ScheduledPipeline]:
    """The pipelines whose trigger is `type: schedule` (anything with `.name` and
    `.spec.trigger`). Invalid expressions raise CronError (the validator reports them first)."""
    out: list[ScheduledPipeline] = []
    for p in pipelines:
        t = p.spec.trigger
        if t.type != "schedule" or not t.schedule:
            continue
        out.append(
            ScheduledPipeline(
                p.name,
                Schedule.parse(t.schedule, getattr(t, "timezone", None)),
                getattr(t, "catchup", None) or DEFAULT_CATCHUP,
            )
        )
    return out


def _scheduled_safe(pipelines: Iterable[Any]) -> list[ScheduledPipeline]:
    """`scheduled`, skipping (and logging) a pipeline whose schedule doesn't parse."""
    out: list[ScheduledPipeline] = []
    for p in pipelines:
        try:
            out += scheduled([p])
        except Exception as e:
            log.warning("schedule: %s has an invalid schedule (%s); not scheduled", p.name, e)
    return out


def next_fire_times(pipelines: Iterable[Any], now: datetime, n: int = 1) -> list[dict[str, Any]]:
    """For `agentline schedules`: each schedule pipeline with its next `n` fire times."""
    rows: list[dict[str, Any]] = []
    for sp in scheduled(pipelines):
        times = sp.schedule.upcoming(now, n)
        tz = sp.schedule.tz
        rows.append(
            {
                "pipeline": sp.name,
                "schedule": sp.schedule.cron.expr,
                "timezone": getattr(tz, "key", "UTC"),
                "catchup": sp.catchup,
                "next": [fire_iso(t) for t in times],
                "next_local": [t.astimezone(tz).isoformat() for t in times],
            }
        )
    return rows


# -- the loop ---------------------------------------------------------------------------------
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ScheduleLoop:
    """One tick at a time (`tick()`), so tests can drive it with a fake clock."""

    app: Any
    pipelines: list[ScheduledPipeline]
    clock: Clock = utc_now
    grace: float = DEFAULT_GRACE
    submitted: list[tuple[str, str, bool]] = field(default_factory=list)  # name, iso, created

    def _exists(self, name: str, t: datetime) -> bool:
        with self.app.ledger.tx() as tx:
            return tx.get_run_by_dedupe(dedupe_key(name, t)) is not None

    def _missed(self, sp: ScheduledPipeline, now: datetime) -> list[datetime]:
        """Fire times at or before `now` and after the cursor, newest last. Without a cursor
        (start-up) walk back until a fire time that already has a run."""
        out: list[datetime] = []
        t = now + timedelta(microseconds=1)
        found = sp.cursor is not None
        for _ in range(LOOKBACK):
            t = sp.schedule.prev_before(t)
            if sp.cursor is not None:
                if t <= sp.cursor:
                    break
            elif self._exists(sp.name, t):
                found = True
                break
            out.append(t)
            if sp.cursor is not None and sp.catchup == "latest":
                break
        out.reverse()
        if not found:  # never fired: start fresh, fire only what is due right now
            return [x for x in out if (now - x).total_seconds() <= self.grace]
        return out

    def _choose(self, sp: ScheduledPipeline, due: list[datetime], now: datetime) -> list[datetime]:
        if sp.catchup == "all":
            return due
        if sp.catchup == "none":
            return [x for x in due if (now - x).total_seconds() <= self.grace]
        return due[-1:]

    def _submit(self, sp: ScheduledPipeline, t: datetime) -> None:
        iso = fire_iso(t)
        sub = self.app.submit_sync(
            sp.name, {"scheduled_for": iso, "pipeline": sp.name}, dedupe_key(sp.name, t)
        )
        self.submitted.append((sp.name, iso, sub.created))
        if sub.created:
            log.info("schedule: submitted %s for %s (run %s)", sp.name, iso, sub.run_id)

    def tick(self) -> datetime | None:
        """Submit whatever is due; return the earliest next fire time."""
        now = _utc(self.clock())
        nxt: datetime | None = None
        for sp in self.pipelines:
            due = self._missed(sp, now)
            chosen = self._choose(sp, due, now)
            skipped = len(due) - len(chosen)
            if skipped:
                log.warning(
                    "schedule: %s skipped %d missed fire time(s) (catchup: %s)",
                    sp.name,
                    skipped,
                    sp.catchup,
                )
            try:
                for t in chosen:
                    self._submit(sp, t)
            except Exception:
                # cursor unchanged: retried on the next tick (dedupe skips what went through)
                log.exception("schedule: submitting %s for %s failed", sp.name, fire_iso(t))
                retry = now + timedelta(seconds=1)
                nxt = retry if nxt is None or retry < nxt else nxt
                continue
            sp.cursor = now
            n = sp.schedule.next_after(now)
            nxt = n if nxt is None or n < nxt else nxt
        return nxt


async def run_schedules(
    app: Any,
    pipelines: Sequence[Any],
    stop: asyncio.Event,
    clock: Clock = utc_now,
    *,
    max_sleep: float = MAX_SLEEP,
    grace: float | None = None,
) -> None:
    """Submit a run for every due fire time of every schedule pipeline until `stop` is set.
    Sleeps until the next fire time, at most `max_sleep` seconds (so clock jumps and a fake
    clock in tests are noticed). `pipelines` may be live (agentline.live): schedules added,
    changed or removed by a reload are followed, each keeping its place (cursor)."""
    from agentline.live import LivePipelines, current, version

    if grace is None:
        grace = max(DEFAULT_GRACE, 2 * max_sleep)
    loop = ScheduleLoop(app, _scheduled_safe(current(pipelines)), clock, grace)
    seen = version(pipelines)
    if not loop.pipelines and not isinstance(pipelines, LivePipelines):
        return
    log.info("schedule loop: %d pipeline(s)", len(loop.pipelines))
    while not stop.is_set():
        if version(pipelines) != seen:  # a live reload: same names keep their cursor
            seen = version(pipelines)
            cursors = {sp.name: sp.cursor for sp in loop.pipelines}
            loop.pipelines = _scheduled_safe(current(pipelines))
            for sp in loop.pipelines:
                sp.cursor = cursors.get(sp.name, sp.cursor)
        try:
            nxt = await asyncio.to_thread(loop.tick)
        except Exception:  # e.g. the ledger is briefly unavailable: try again soon
            log.exception("schedule loop: tick failed")
            nxt = _utc(clock()) + timedelta(seconds=1)
        wait = max_sleep
        if nxt is not None:
            wait = min(max_sleep, max(0.0, (nxt - _utc(clock())).total_seconds()))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(wait, 0.001))


__all__ = [
    "CATCHUP_POLICIES",
    "Cron",
    "CronError",
    "Schedule",
    "ScheduleLoop",
    "dedupe_key",
    "fire_iso",
    "load_tz",
    "next_fire_times",
    "run_schedules",
    "scheduled",
]
