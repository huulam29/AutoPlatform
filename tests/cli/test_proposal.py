"""approve --proposal: the TOML section writer, the build-unavailable and build paths."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agentline.cli.proposal import header_keys, write_images_table
from agentline.config import Config
from agentline.errors import ValidationError
from agentline.workspaces.images import resolve_image
from tests.cli.helpers import CONFIG, Cli
from tests.fakebin import fake_bin

# -- the writer -------------------------------------------------------------------------------


def test_replace_keeps_everything_else_byte_identical() -> None:
    new = write_images_table(
        CONFIG, "python", {"from": "agentline/runner-base", "apt": ["python3", "psql"]}, "c"
    )
    head, _, _ = CONFIG.partition("[images.python]\n")
    tail = CONFIG[CONFIG.index("\n# node toolchain") :]
    assert new.startswith(head + "[images.python]\n# c\n")
    assert new.endswith(tail)  # the next table's leading comment stays with it
    data = tomllib.loads(new)
    assert data["images"]["python"]["apt"] == ["python3", "psql"]
    assert data["images"]["node"] == {"apt": ["nodejs"]}
    assert data["environments"] == tomllib.loads(CONFIG)["environments"]


def test_append_new_table_and_quoting() -> None:
    new = write_images_table(CONFIG, "node+python", {"apt": ["nodejs", "x"]})
    assert new.startswith(CONFIG)
    assert new[len(CONFIG) :] == '\n[images."node+python"]\napt = ["nodejs", "x"]\n'
    assert tomllib.loads(new)["images"]["node+python"]["apt"] == ["nodejs", "x"]
    # no trailing newline, empty file
    assert (
        write_images_table("a = 1", "go", {"apt": ["g"]}) == 'a = 1\n\n[images.go]\napt = ["g"]\n'
    )
    assert write_images_table("", "go", {"apt": ["g"]}) == '[images.go]\napt = ["g"]\n'


def test_header_last_line_and_quoted_header() -> None:
    text = 'x = 1\n[images."python"]'
    new = write_images_table(text, "python", {"apt": ["a"]})
    assert new == 'x = 1\n[images."python"]\napt = ["a"]\n'
    assert header_keys("[ images . 'py' ]  # c") == (False, ["images", "py"])
    assert header_keys("[[images.x]]") == (True, ["images", "x"])
    assert header_keys('  ["a", "b"],') is None
    assert header_keys("[images.x]]") is None


def test_refuses_inline_or_dotted_definitions() -> None:
    with pytest.raises(ValidationError, match="by hand"):
        write_images_table('[images]\npython = { apt = ["a"] }\n', "python", {"apt": ["b"]})
    with pytest.raises(ValidationError):
        write_images_table('images.python.apt = ["a"]\n', "python", {"apt": ["b"]})


def test_subtables_are_left_alone() -> None:
    text = '[images.python]\napt = ["a"]\n\n[images.python.extra]\nk = "v"\n'
    new = write_images_table(text, "python", {"apt": ["a", "b"], "extra": {"k": "v"}})
    assert new == '[images.python]\napt = ["a", "b"]\n\n[images.python.extra]\nk = "v"\n'


# -- end to end -------------------------------------------------------------------------------


def _needs_proposal(cli: Cli) -> tuple[str, str]:
    run_id = cli.submit("missing")
    cli.ok("worker", "--until-idle")
    v = cli.json("runs", "show", run_id)
    ex = v["executions"][0]
    assert ex["status"] == "needs-review" and ex["reason"] == "missing_tools"
    pid = ex["detail"]["image_proposal"]["id"]
    assert pid.startswith(f"p-{ex['id']}-") and len(pid.rsplit("-", 1)[1]) == 8
    assert f"agentline approve {run_id} --proposal {pid}" in v["next"][0]
    return run_id, pid


def test_approve_proposal_build_unavailable(cli: Cli) -> None:
    run_id, pid = _needs_proposal(cli)
    before = cli.config.read_text()
    out = cli.ok("approve", run_id, "--proposal", pid, "--comment", "fine")
    assert "image build unavailable" in out and "queued for retry" in out
    assert "+apt = [" in out  # the diff is shown

    assert (cli.root / "agentline.toml.bak").read_text() == before
    after = cli.config.read_text()
    head = before[: before.index("[images.python]")]
    assert after.startswith(head) and after.endswith(before[before.index("\n# node toolchain") :])
    cfg = Config.load(cli.config)
    assert cfg.images["python"]["apt"] == ["python3", "python3-venv", "postgresql-client"]

    v = cli.json("runs", "show", run_id)
    assert v["executions"][0]["status"] == "ready"
    actions = [a["action"] for a in v["audit"]]
    assert actions == ["approve_proposal", "resolve"]
    applied = v["audit"][0]["detail"]
    assert applied["proposal"] == pid and "+apt = [" in applied["diff"]
    assert applied["backup"].endswith("agentline.toml.bak") and v["audit"][0]["actor"] == "alice"
    resolved = v["audit"][1]["detail"]
    assert resolved["action"] == "retry" and resolved["build"]["status"] == "unavailable"

    code, _, err = cli.run("approve", run_id, "--proposal", pid)  # retry cleared it
    assert code == 1 and f"no proposal {pid}" in err
    assert cli.config.read_text() == after


def test_approve_proposal_builds_the_retry_image(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "docker.log"
    script = tmp_path / "fake_docker.py"
    script.write_text(
        "import sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['image', 'inspect']: sys.exit(1)\n"
        "sys.stdin.read()\n"
    )
    docker = fake_bin(tmp_path, "docker", script=script)
    monkeypatch.setenv("AGENTLINE_DOCKER", str(docker))
    run_id, pid = _needs_proposal(cli)
    out = cli.ok("approve", run_id, "--proposal", pid)
    # the tag built is exactly what the container provider resolves after the change
    cfg = Config.load(cli.config)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "pyproject.toml").write_text("")
    choice = resolve_image("auto", cfg.repos["app"], cfg, worktree)
    assert choice.profile == "python" and "postgresql-client" in (choice.dockerfile or "")
    calls = log.read_text().splitlines()
    assert calls[0] == f"image inspect {choice.ref}"
    assert calls[1].startswith(f"build -t {choice.ref} -f -")
    assert f"image {choice.ref} built" in out
    v = cli.json("runs", "show", run_id)
    assert v["audit"][-1]["detail"]["build"] == {"status": "ok", "tag": choice.ref, "built": True}


def test_proposal_without_packages_is_refused(cli: Cli) -> None:
    from agentline.cli.proposal import proposed_table

    with pytest.raises(ValidationError, match="no packages"):
        proposed_table(Config(), {"profile_from": "python", "add_apt": [], "unknown": ["zz"]})
    with pytest.raises(ValidationError, match="base image"):
        proposed_table(Config(), {"profile_from": "base", "add_apt": ["x"]})
    # a builtin profile without a table gets the builtin packages plus the new ones
    profile, table = proposed_table(Config(), {"profile_from": "node", "add_apt": ["jq"]})
    assert (profile, table) == ("node", {"apt": ["nodejs", "npm", "jq"]})
