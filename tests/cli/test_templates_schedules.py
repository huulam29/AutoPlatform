"""agentline templates / new / schedules, and the schedule loop in `worker`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentline.builtins import default_registry
from agentline.pipeline import load_spec, validate_spec
from agentline.templates import copy_template, list_templates, render_pipeline
from tests.cli.helpers import Cli

TEMPLATES = ("feature-factory", "helpdesk", "nightly-tests", "pr-review", "ticket-to-eod")


def test_templates_lists_all_with_descriptions(cli: Cli) -> None:
    out = cli.ok("templates")
    for name in TEMPLATES:
        assert name in out
    rows = cli.json("templates")
    assert [r["name"] for r in rows] == list(TEMPLATES)
    assert all(len(r["description"]) > 10 for r in rows)


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_template_validates_as_shipped(name: str) -> None:
    t = next(t for t in list_templates() if t.name == name)
    path = Path(str(t.root / "pipeline.yaml"))
    spec = load_spec(path)
    assert spec.name == name
    assert validate_spec(spec, default_registry()) == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_new_copies_and_validates(cli: Cli, name: str) -> None:
    dest = cli.root / "out"
    out = cli.ok("new", name, str(dest))
    assert f"{dest / name / 'pipeline.yaml'}: ok ({name}," in out
    code, vout, _ = cli.run("validate", str(dest / name / "pipeline.yaml"))
    assert code == 0, vout


def test_new_substitutes_name_and_copies_files(cli: Cli) -> None:
    dest = cli.root / "pipelines2"
    cli.ok("new", "ticket-to-eod", str(dest), "--name", "acme-eod")
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == [
        "acme-eod/pipeline.yaml",
        "acme-eod/prompts/implement.md",
        "acme-eod/prompts/plan.md",
        "acme-eod/schemas/plan.json",
    ]
    text = (dest / "acme-eod" / "pipeline.yaml").read_text()
    assert "\nname: acme-eod\n" in text and "name: ticket-to-eod" not in text
    assert text.startswith("# Jira ticket assigned")  # comments survive
    assert load_spec(dest / "acme-eod" / "pipeline.yaml").name == "acme-eod"


def test_new_refuses_to_overwrite_unless_force(cli: Cli) -> None:
    dest = cli.root / "out"
    cli.ok("new", "pr-review", str(dest))
    prompt = dest / "pr-review" / "prompts" / "review.md"
    prompt.write_text("my edits")
    code, _, err = cli.run("new", "pr-review", str(dest))
    assert code == 1 and "refusing to overwrite" in err and "--force" in err
    assert prompt.read_text() == "my edits"  # nothing written
    cli.ok("new", "pr-review", str(dest), "--force")
    assert prompt.read_text() != "my edits"
    # a different name gets its own folder: nothing shared, nothing collides
    cli.ok("new", "pr-review", str(dest), "--name", "other")
    assert (dest / "other" / "pipeline.yaml").exists() and prompt.read_text() != "my edits"


def test_new_errors(cli: Cli) -> None:
    code, _, err = cli.run("new", "nope", str(cli.root / "x"))
    assert code == 1 and "unknown template 'nope'" in err and "nightly-tests" in err
    code, _, err = cli.run("new", "nightly-tests", str(cli.root / "x"), "--name", "a b")
    assert code == 1 and "invalid pipeline name" in err


def test_render_pipeline_only_touches_top_level_name() -> None:
    text = "version: 1\nname: x\nnodes:\n  a:\n    name: keep\n"
    assert render_pipeline(text, "y") == "version: 1\nname: y\nnodes:\n  a:\n    name: keep\n"


def test_copy_template_into_pipelines_dir_is_loadable(cli: Cli) -> None:
    copy_template("nightly-tests", cli.pipelines, name="nightly")
    out = cli.ok("schedules")
    assert "nightly  0 2 * * *  (UTC, catchup: latest)" in out
    assert out.count("T02:00:00Z") == 3


def test_schedules_json_and_empty(cli: Cli) -> None:
    assert "no pipelines with a schedule trigger" in cli.ok("schedules")
    (cli.pipelines / "berlin.yaml").write_text(
        "version: 1\nname: berlin\n"
        "trigger: { type: schedule, schedule: '30 6 * * mon-fri', timezone: Europe/Berlin,"
        " catchup: none }\n"
        "nodes:\n  a: { type: action, uses: shell.run, with: { cmd: 'true' } }\n"
    )
    rows = cli.json("schedules", "--count", "2")
    assert len(rows) == 1
    r = rows[0]
    assert (r["pipeline"], r["timezone"], r["catchup"]) == ("berlin", "Europe/Berlin", "none")
    assert len(r["next"]) == 2 and all(x.endswith("Z") for x in r["next"])
    assert all("T06:30:00+0" in x for x in r["next_local"])
    assert "(20" in cli.ok("schedules")  # local times shown for non-UTC zones


def test_invalid_cron_is_reported_by_validate(cli: Cli) -> None:
    path = cli.root / "bad.yaml"
    path.write_text(
        "version: 1\nname: bad\ntrigger:\n  type: schedule\n  schedule: '0 25 * * *'\n"
        "nodes:\n  a: { type: action, uses: shell.run, with: { cmd: 'true' } }\n"
    )
    code, out, _ = cli.run("validate", str(path))
    assert code == 1
    assert f"{path}:5:1: trigger.schedule: invalid schedule: hour: 25 is out of range" in out


def test_worker_runs_the_schedule_loop(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    copy_template("nightly-tests", cli.pipelines, name="nightly")
    seen: dict[str, Any] = {}

    async def fake_run_schedules(app: Any, pipelines: Any, stop: asyncio.Event) -> None:
        seen["names"] = sorted(p.name for p in pipelines)
        stop.set()  # stops the worker and poller too

    monkeypatch.setattr("agentline.cli.commands.run_schedules", fake_run_schedules)
    code, _, err = cli.run("worker")
    assert code == 0
    assert "nightly" in seen["names"]
    assert "schedule nightly: 0 2 * * * next" in err
