"""Cron parsing, next/prev fire times, and DST handling with zoneinfo."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from agentline.schedules import Cron, CronError, Schedule


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def nxt(expr: str, start: datetime, n: int = 1, tz: str | None = None) -> list[datetime]:
    return Schedule.parse(expr, tz).upcoming(start, n)


def test_fields() -> None:
    c = Cron.parse("*/15 9-17/4 1,15 jan-mar mon-fri")
    assert c.minutes == {0, 15, 30, 45}
    assert c.hours == {9, 13, 17}
    assert c.days == {1, 15}
    assert c.months == {1, 2, 3}
    assert c.weekdays == {1, 2, 3, 4, 5}
    assert Cron.parse("5/20 * * * *").minutes == {5, 25, 45}
    assert Cron.parse("0 0 * * 7").weekdays == {0}  # 7 is Sunday too
    assert Cron.parse("0 0 * * SUN,sat").weekdays == {0, 6}


@pytest.mark.parametrize(
    ("alias", "expr"),
    [("@hourly", "0 * * * *"), ("@daily", "0 0 * * *"), ("@midnight", "0 0 * * *")],
)
def test_aliases(alias: str, expr: str) -> None:
    a, b = Cron.parse(alias), Cron.parse(expr)
    assert (a.minutes, a.hours, a.days, a.months, a.weekdays) == (
        b.minutes,
        b.hours,
        b.days,
        b.months,
        b.weekdays,
    )
    assert Cron.parse("@weekly").weekdays == {0}
    assert Cron.parse("@yearly").months == {1}


@pytest.mark.parametrize(
    ("expr", "msg"),
    [
        ("", "empty"),
        ("* * * *", "expected 5 fields"),
        ("* * * * * *", "expected 5 fields"),
        ("60 * * * *", "out of range"),
        ("* 24 * * *", "out of range"),
        ("* * 0 * *", "out of range"),
        ("* * * 13 *", "out of range"),
        ("* * * * 8", "out of range"),
        ("*/0 * * * *", "bad step"),
        ("5-1 * * * *", "backwards"),
        ("a * * * *", "not a number"),
        ("1,,2 * * * *", "empty list item"),
        ("@reboot", "unknown alias"),
        ("0 0 30 2 *", "never occurs"),
        ("0 0 31 4,6 *", "never occurs"),
    ],
)
def test_invalid(expr: str, msg: str) -> None:
    with pytest.raises(CronError, match=msg):
        Cron.parse(expr)


def test_next_basic() -> None:
    assert nxt("*/15 * * * *", utc(2026, 1, 1, 10, 7, 30), 2) == [
        utc(2026, 1, 1, 10, 15),
        utc(2026, 1, 1, 10, 30),
    ]
    # strictly after: exactly on a fire time gives the next one
    assert nxt("0 * * * *", utc(2026, 1, 1, 10, 0)) == [utc(2026, 1, 1, 11, 0)]
    assert nxt("@daily", utc(2026, 12, 31, 23, 59, 59)) == [utc(2027, 1, 1)]
    assert nxt("0 9 * * mon", utc(2026, 10, 19, 9, 0)) == [utc(2026, 10, 26, 9, 0)]  # a Monday


def test_leap_day_and_month_ends() -> None:
    assert nxt("0 0 29 2 *", utc(2026, 3, 1)) == [utc(2028, 2, 29)]
    assert nxt("0 0 31 * *", utc(2026, 4, 1), 2) == [utc(2026, 5, 31), utc(2026, 7, 31)]


def test_dom_dow_or_rule() -> None:
    # both restricted: the 13th OR any Friday
    got = nxt("0 0 13 * fri", utc(2026, 11, 1), 3)
    assert got == [utc(2026, 11, 6), utc(2026, 11, 13), utc(2026, 11, 20)]
    # dom is `*`: only Fridays (AND with an unrestricted field)
    assert nxt("0 0 * * fri", utc(2026, 11, 1)) == [utc(2026, 11, 6)]


def test_prev_before() -> None:
    s = Schedule.parse("*/15 * * * *")
    assert s.prev_before(utc(2026, 1, 1, 10, 7)) == utc(2026, 1, 1, 10, 0)
    assert s.prev_before(utc(2026, 1, 1, 10, 0)) == utc(2026, 1, 1, 9, 45)  # strictly before
    assert s.prev_before(utc(2026, 1, 1, 10, 0, 1)) == utc(2026, 1, 1, 10, 0)
    assert Schedule.parse("0 0 29 2 *").prev_before(utc(2027, 1, 1)) == utc(2024, 2, 29)


def test_timezone() -> None:
    got = nxt("0 9 * * *", utc(2026, 1, 15), 1, "Europe/Berlin")
    assert got == [utc(2026, 1, 15, 8, 0)]  # 09:00 CET
    got = nxt("0 9 * * *", utc(2026, 7, 15), 1, "Europe/Berlin")
    assert got == [utc(2026, 7, 15, 7, 0)]  # 09:00 CEST


NY = "America/New_York"


def test_dst_spring_forward_gap_fires_once_shifted() -> None:
    # 2026-03-08 02:00 EST -> 03:00 EDT; 02:30 doesn't exist that day
    got = nxt("30 2 * * *", utc(2026, 3, 7, 12), 3, NY)
    assert got == [
        utc(2026, 3, 8, 7, 30),  # = 03:30 EDT, shifted by the gap
        utc(2026, 3, 9, 6, 30),  # 02:30 EDT
        utc(2026, 3, 10, 6, 30),
    ]
    # every minute: the gap minutes collapse onto real ones, no instant fires twice
    s = Schedule.parse("* * * * *", NY)
    times = s.upcoming(utc(2026, 3, 8, 6, 58), 4)
    assert times == [
        utc(2026, 3, 8, 6, 59),
        utc(2026, 3, 8, 7, 0),
        utc(2026, 3, 8, 7, 1),
        utc(2026, 3, 8, 7, 2),
    ]


def test_dst_fall_back() -> None:
    # 2026-11-01 02:00 EDT -> 01:00 EST; 01:30 happens twice
    got = nxt("30 1 * * *", utc(2026, 10, 31, 12), 2, NY)
    assert got == [utc(2026, 11, 1, 5, 30), utc(2026, 11, 2, 6, 30)]  # first pass only
    # hour field `*`: fires on both passes, so it stays hourly in real time
    got = nxt("0 * * * *", utc(2026, 11, 1, 4, 30), 4, NY)
    assert got == [
        utc(2026, 11, 1, 5),  # 01:00 EDT
        utc(2026, 11, 1, 6),  # 01:00 EST
        utc(2026, 11, 1, 7),  # 02:00 EST
        utc(2026, 11, 1, 8),
    ]


def test_dst_next_and_prev_agree() -> None:
    for expr in ("30 1 * * *", "30 2 * * *", "0 * * * *", "*/20 * * * *"):
        s = Schedule.parse(expr, NY)
        for start in (utc(2026, 3, 8, 3), utc(2026, 10, 31, 22)):
            times = s.upcoming(start, 30)
            assert times == sorted(set(times))
            for a, b in itertools.pairwise(times):
                assert s.prev_before(b) == a
                assert s.next_after(a) == b
            assert s.prev_before(times[0]) <= start


def test_aware_times_required() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Schedule.parse("@daily").next_after(datetime(2026, 1, 1))


def test_unknown_timezone() -> None:
    with pytest.raises(CronError, match="unknown timezone"):
        Schedule.parse("@daily", "Mars/Olympus")


def test_upcoming_is_fast_for_dense_schedules() -> None:
    s = Schedule.parse("* * * * *", NY)
    start = utc(2026, 11, 1, 4)
    times = s.upcoming(start, 300)
    assert times[-1] - times[0] == timedelta(minutes=299)
