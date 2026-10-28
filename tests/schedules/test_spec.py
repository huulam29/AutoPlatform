"""Schedule trigger fields in the pipeline spec, and cron validation with line numbers."""

from __future__ import annotations

import pytest

from agentline.pipeline import PipelineSpec, SpecError, parse_spec, validate_spec

BASE = """\
version: 1
name: s
trigger:
{trigger}
nodes:
  a: {{ type: action, uses: shell.run, with: {{ cmd: "true" }} }}
"""


def spec(trigger: str) -> PipelineSpec:
    return parse_spec(BASE.format(trigger=trigger))


def issues(trigger: str) -> list[str]:
    try:
        s = spec(trigger)
    except SpecError as e:
        return [str(i) for i in e.issues]
    return [str(i) for i in validate_spec(s)]


def test_fields_parsed() -> None:
    s = spec('  type: schedule\n  schedule: "@daily"\n  timezone: Europe/Berlin\n  catchup: all')
    t = s.trigger
    assert (t.schedule, t.timezone, t.catchup, t.line) == ("@daily", "Europe/Berlin", "all", 5)
    assert validate_spec(s) == []
    assert spec('  type: schedule\n  schedule: "0 2 * * *"').trigger.catchup == "latest"


def test_bad_cron_reported_with_line() -> None:
    got = issues('  type: schedule\n  timezone: UTC\n  schedule: "61 * * * *"')
    assert len(got) == 1
    assert got[0].startswith("<pipeline>:6:1: trigger.schedule: invalid schedule: minute: 61")
    assert "hint: cron is" in got[0]


def test_bad_timezone() -> None:
    got = issues('  type: schedule\n  schedule: "@daily"\n  timezone: Mars/Base')
    assert len(got) == 1 and "trigger.timezone: invalid timezone" in got[0]


@pytest.mark.parametrize(
    ("trigger", "msg"),
    [
        ('  type: schedule\n  schedule: "@daily"\n  catchup: some', "unknown catchup policy"),
        ("  type: schedule\n  schedule: 5", "schedule must be a string"),
        ("  type: manual\n  catchup: all", "'catchup' only applies to schedule triggers"),
        ('  type: webhook\n  source: jira\n  event: x\n  schedule: "@daily"', "only applies"),
        ("  type: schedule", "schedule triggers need 'schedule'"),
    ],
)
def test_parse_errors(trigger: str, msg: str) -> None:
    got = issues(trigger)
    assert any(msg in x for x in got), got
