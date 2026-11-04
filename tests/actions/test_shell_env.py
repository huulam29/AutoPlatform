"""shell.run, env.deploy and env.test: substitution, cwd, check, and crash behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentline.actions.env import substitute
from agentline.config import Config
from agentline.errors import PermanentError
from agentline.registry import RunRequest
from tests.fakebin import posix_shell

from .conftest import Harness
from .fakes import Repos


def shell_yaml(with_: str, max_attempts: int = 2) -> str:
    return f"""
version: 1
name: sh
nodes:
  step: {{ type: action, uses: shell.run, out: res, max_attempts: {max_attempts}, with: {with_} }}
"""


@posix_shell
def test_shell_run_success(h: Harness, tmp_path: Path) -> None:
    h.pipeline(shell_yaml('{ cmd: "echo hi $FOO; echo err >&2", env: { FOO: bar } }'))
    run = h.submit("sh")
    h.drain()
    res = h.app.result(run, "step")
    assert res["exit_code"] == 0
    assert res["stdout_tail"] == "hi bar\n" and res["stderr_tail"] == "err\n"


@posix_shell
def test_shell_run_list_cmd_and_cwd(h: Harness, tmp_path: Path) -> None:
    h.pipeline(shell_yaml(f'{{ cmd: ["pwd"], cwd: "{tmp_path}" }}'))
    run = h.submit("sh")
    h.drain()
    assert Path(h.app.result(run, "step")["stdout_tail"].strip()).resolve() == tmp_path.resolve()


@posix_shell
def test_shell_nonzero_retries_then_dies(h: Harness, tmp_path: Path) -> None:
    log = tmp_path / "count"
    h.pipeline(shell_yaml(f'{{ cmd: "echo x >> {log}; exit 3" }}', max_attempts=2))
    run = h.submit("sh")
    h.drain()
    ex = h.execution(run, "step#1")
    assert ex.status == "dead" and "exited with 3" in (ex.error or "")
    assert log.read_text().count("x") == 2  # a finished command is recorded; a retry reruns it
    assert h.effects(run) == {"step#1:run#1": "committed", "step#1:run#2": "committed"}


@posix_shell
def test_shell_permanent_failure(h: Harness, tmp_path: Path) -> None:
    log = tmp_path / "count"
    h.pipeline(shell_yaml(f'{{ cmd: "echo x >> {log}; exit 1", permanent: true }}', 3))
    run = h.submit("sh")
    h.drain()
    assert h.execution(run, "step#1").status == "dead"
    assert log.read_text().count("x") == 1


def test_shell_timeout(h: Harness) -> None:
    h.pipeline(shell_yaml('{ cmd: "sleep 5", timeout: 0.2 }', max_attempts=1))
    run = h.submit("sh")
    h.drain()
    assert "timed out" in (h.execution(run, "step#1").error or "")


@posix_shell
def test_shell_crash_mid_run_halts(h: Harness, tmp_path: Path) -> None:
    log = tmp_path / "count"
    h.pipeline(shell_yaml(f'{{ cmd: "echo x >> {log}" }}'))
    run = h.submit("sh")
    h.leave_intent(run, "step", "run#1")  # the worker died while the command ran
    h.drain()
    ex = h.execution(run, "step#1")
    assert ex.status == "needs-review" and ex.reason == "effect_unknown"
    assert not log.exists()


@posix_shell
def test_shell_idempotent_retries_after_crash(h: Harness, tmp_path: Path) -> None:
    log = tmp_path / "count"
    h.pipeline(shell_yaml(f'{{ cmd: "echo x >> {log}", idempotent: true }}'))
    run = h.submit("sh")
    h.leave_intent(run, "step", "run#1")
    h.drain()
    assert h.status(run) == "done"
    assert log.read_text() == "x\n"


@posix_shell
def test_secrets_are_scrubbed_from_output(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_super_secret_value")
    h.pipeline(shell_yaml('{ cmd: "echo token=$GITHUB_TOKEN", pass_env: [GITHUB_TOKEN] }'))
    run = h.submit("sh")
    h.drain()
    assert h.app.result(run, "step")["stdout_tail"] == "token=***\n"


# -- env.deploy / env.test ------------------------------------------------------------------
DEPLOY_YAML = """
version: 1
name: ship
nodes:
  build: { type: agent, runner: fn, prompt: "go", out: branch }
  approve: { type: gate, prompt: "ship?" }
  deploy: { type: action, uses: env.deploy, with: { env: eod }, in: [branch], out: deployment }
  test: { type: action, uses: env.test, with: { env: eod }, in: [deployment], out: report }
edges:
  - build -> approve -> deploy -> test
"""


def fixed_branch(name: str, sha: str, mirror: str | None = None) -> Any:
    async def agent(req: RunRequest) -> dict[str, Any]:
        out = {"name": name, "sha": sha, "base_sha": "", "repo": "app"}
        if mirror:
            out["mirror"] = mirror
        return out

    return agent


def eod_config(tmp: Path, *, check: bool = True, test: str = "test -f current") -> Config:
    env: dict[str, Any] = {
        "deploy": "echo {branch} {sha} >> deploys.log && echo {sha} > current",
        "test": test,
        "cwd": str(tmp),
    }
    if check:
        env["check"] = "cat current"
    return Config.from_dict({"environments": {"eod": env}})


def ship(h: Harness, run: str) -> None:
    h.drain()
    asyncio.run(h.app.approve(run, actor="tester"))
    h.drain()


def test_substitute_quotes_untrusted_values() -> None:
    assert substitute("d {branch} {sha}", {"branch": "a/b", "sha": "abc"}) == "d a/b abc"
    assert substitute("d {branch}", {"branch": "x; rm -rf /"}) == "d 'x; rm -rf /'"
    assert substitute("awk '{print}' {sha}", {"sha": "1"}) == "awk '{print}' 1"
    with pytest.raises(PermanentError, match="sha"):
        substitute("d {sha}", {"sha": None})


@posix_shell
def test_deploy_substitution_and_test(h: Harness, tmp_path: Path) -> None:
    h.pipeline(
        DEPLOY_YAML, agent=fixed_branch("feat;echo pwned", "abc123"), config=eod_config(tmp_path)
    )
    run = h.submit("ship")
    ship(h, run)
    assert h.status(run) == "done"
    assert (tmp_path / "deploys.log").read_text() == "feat;echo pwned abc123\n"
    dep = h.app.result(run, "deploy")
    assert dep["env"] == "eod" and dep["sha"] == "abc123" and dep["branch"] == "feat;echo pwned"
    assert dep["exit_code"] == 0 and dep["at"] > 0
    report = h.app.result(run, "test")
    assert report == {
        "env": "eod",
        "sha": "abc123",
        "passed": True,
        "exit_code": 0,
        "output_tail": "",
    }


@posix_shell
def test_failing_tests_are_a_result(h: Harness, tmp_path: Path) -> None:
    h.pipeline(
        DEPLOY_YAML,
        agent=fixed_branch("f", "abc"),
        config=eod_config(tmp_path, test="echo boom; exit 4"),
    )
    run = h.submit("ship")
    ship(h, run)
    report = h.app.result(run, "test")
    assert report["passed"] is False and report["exit_code"] == 4
    assert "boom" in report["output_tail"]


def test_deploy_crash_check_finds_deployed_sha(h: Harness, tmp_path: Path) -> None:
    """Crash after the deploy ran: the check prints the deployed sha, so no second deploy."""
    (tmp_path / "current").write_text("abc123\n")
    h.pipeline(DEPLOY_YAML, agent=fixed_branch("f", "abc123"), config=eod_config(tmp_path))
    run = h.submit("ship")
    h.leave_intent(run, "deploy", "deploy#1")
    ship(h, run)
    assert h.status(run) == "done"
    assert not (tmp_path / "deploys.log").exists()
    assert h.app.result(run, "deploy")["verified_by"] == "check"


@posix_shell
def test_deploy_crash_check_mismatch_redeploys(h: Harness, tmp_path: Path) -> None:
    (tmp_path / "current").write_text("oldsha\n")
    h.pipeline(DEPLOY_YAML, agent=fixed_branch("f", "abc123"), config=eod_config(tmp_path))
    run = h.submit("ship")
    h.leave_intent(run, "deploy", "deploy#1")
    ship(h, run)
    assert h.status(run) == "done"
    assert (tmp_path / "deploys.log").read_text() == "f abc123\n"


def test_deploy_crash_without_check_halts(h: Harness, tmp_path: Path) -> None:
    h.pipeline(
        DEPLOY_YAML, agent=fixed_branch("f", "abc123"), config=eod_config(tmp_path, check=False)
    )
    run = h.submit("ship")
    h.leave_intent(run, "deploy", "deploy#1")
    ship(h, run)
    ex = h.execution(run, "deploy#1")
    assert ex.status == "needs-review" and ex.reason == "effect_unknown"
    assert h.status(run) == "needs-review"
    assert not (tmp_path / "deploys.log").exists()


def test_deploy_is_deploy_class_and_rejects_ai(h: Harness, tmp_path: Path) -> None:
    from agentline.pipeline import SpecError

    bad = DEPLOY_YAML.replace(
        "with: { env: eod }, in: [branch]",
        "with: { env: eod }, effect: { on_unknown: ai }, in: [branch]",
    )
    with pytest.raises(SpecError, match="ai"):
        h.pipeline(bad, agent=fixed_branch("f", "a"), config=eod_config(tmp_path))
    ungated = DEPLOY_YAML.replace("build -> approve -> deploy", "build -> deploy").replace(
        '  approve: { type: gate, prompt: "ship?" }\n', ""
    )
    with pytest.raises(SpecError, match="gate"):
        h.pipeline(ungated, agent=fixed_branch("f", "a"), config=eod_config(tmp_path))


def test_deploy_in_temp_worktree_of_sha(h: Harness, tmp_path: Path, repos: Repos) -> None:
    sha = repos.commit("app.txt", "v2", "feature")
    out = tmp_path / "out"
    out.mkdir()
    config = Config.from_dict(
        {
            "environments": {
                "eod": {
                    "deploy": f"git rev-parse HEAD > {out}/head && cat app.txt > {out}/content"
                    f" && pwd > {out}/pwd",
                    "test": "true",
                }
            }
        }
    )
    repos.commit("other.txt", "later", "feature")  # the mirror moved on; the sha didn't
    h.pipeline(DEPLOY_YAML, agent=fixed_branch("feature", sha, str(repos.mirror)), config=config)
    run = h.submit("ship")
    ship(h, run)
    assert h.status(run) == "done"
    assert (out / "head").read_text().strip() == sha
    assert (out / "content").read_text() == "v2"
    assert not Path((out / "pwd").read_text().strip()).exists()  # worktree removed


def test_unknown_environment(h: Harness, tmp_path: Path) -> None:
    h.pipeline(
        DEPLOY_YAML.replace("env: eod }, in: [branch]", "env: prod }, in: [branch]"),
        agent=fixed_branch("f", "a"),
        config=eod_config(tmp_path),
    )
    run = h.submit("ship")
    ship(h, run)
    ex = h.execution(run, "deploy#1")
    assert ex.status == "dead" and ex.attempt == 1 and "prod" in (ex.error or "")


@posix_shell
def test_script_next_to_the_pipeline_reads_inputs_and_returns_json(h: Harness) -> None:
    scripts = h.tmp / "scripts"
    scripts.mkdir(exist_ok=True)
    script = scripts / "count.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "inputs = json.load(open(os.environ['AGENTLINE_INPUTS']))\n"
        "print('working', file=sys.stderr)\n"
        "print(json.dumps({'words': len(inputs['note']['text'].split()), 'arg': sys.argv[1]}))\n"
    )
    script.chmod(0o755)
    h.pipeline(
        """
version: 1
name: sh
nodes:
  note:
    type: action
    uses: shell.run
    out: note
    with: { cmd: 'printf "{\\"text\\": \\"a b c\\"}"', output: json }
  count:
    type: action
    uses: shell.run
    in: [note]
    out: res
    with: { cmd: '"{{pipeline.dir}}/scripts/count.py" {{note.text}}', output: json }
edges:
  - note -> count
"""
    )
    run = h.submit("sh")
    h.drain()
    res = h.app.result(run, "count")
    assert res["words"] == 3 and res["arg"] == "a" and res["stderr_tail"] == "working\n"


def test_output_json_must_be_json(h: Harness) -> None:
    h.pipeline(shell_yaml('{ cmd: "echo not json", output: json }', max_attempts=3))
    run = h.submit("sh")
    h.drain()
    assert h.status(run) == "dead"


@posix_shell
def test_manual_retry_reruns_the_command(h: Harness) -> None:
    marker = h.tmp / "fixed"
    h.pipeline(shell_yaml(f'{{ cmd: "test -f {marker}" }}', max_attempts=1))
    run = h.submit("sh")
    h.drain()
    assert h.status(run) == "dead"
    marker.write_text("")
    asyncio.run(h.app.retry(run))
    h.drain()
    assert h.status(run) == "done"  # not the failure recorded before the retry


def test_manual_retry_redeploys(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry after a failed deploy must deploy again, not reuse the recorded failure."""
    from agentline.actions import env as env_action

    calls: list[str] = []
    exit_code = {"deploy": 1}

    async def fake_run(cmd: Any, **kw: Any) -> tuple[int, str, str]:
        calls.append(str(cmd))
        return (exit_code["deploy"] if "deploy" in str(cmd) else 0), "", ""

    monkeypatch.setattr(env_action, "run_cmd", fake_run)
    config = Config.from_dict({"environments": {"eod": {"deploy": "deploy {sha}", "test": "t"}}})
    h.pipeline(DEPLOY_YAML.replace("out: deployment }", "out: deployment, max_attempts: 1 }"),
               agent=fixed_branch("f", "abc123"), config=config)  # fmt: skip
    run = h.submit("ship")
    ship(h, run)
    assert h.status(run) == "dead" and calls == ["deploy abc123"]
    exit_code["deploy"] = 0  # the environment was fixed
    asyncio.run(h.app.retry(run))
    h.drain()
    assert h.status(run) == "done"
    assert calls == ["deploy abc123", "deploy abc123", "t"]
    audits = [a.detail for a in h.app.audit_log(run) if a.action == "deploy"]
    assert [(a["effect"], a["exit_code"]) for a in audits] == [("deploy#1", 1), ("deploy#r1.1", 0)]


def test_host_commands_see_only_basics_and_what_they_declare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentline.actions._util import command_env, pass_env_names

    monkeypatch.setenv("SOME_API_TOKEN", "not-for-commands")
    monkeypatch.setenv("DEPLOY_REGION", "eu")
    env = command_env({"X": "1"}, ["DEPLOY_REGION", "UNSET_NAME"])
    assert env["X"] == "1" and env["DEPLOY_REGION"] == "eu"
    assert "SOME_API_TOKEN" not in env and "UNSET_NAME" not in env
    assert "SOME_API_TOKEN" not in command_env()
    assert pass_env_names("ONE", "w") == ["ONE"] and pass_env_names(None, "w") == []
    with pytest.raises(PermanentError, match="pass_env"):
        pass_env_names({"a": 1}, "shell.run")


def test_deploy_passes_only_the_env_pass_env(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentline.actions import env as env_action

    seen: list[list[str]] = []

    async def fake_run(cmd: Any, **kw: Any) -> tuple[int, str, str]:
        seen.append(list(kw.get("pass_env") or ()))
        return 0, "", ""

    monkeypatch.setattr(env_action, "run_cmd", fake_run)
    eod = {"deploy": "deploy {sha}", "test": "t", "pass_env": ["AWS_PROFILE"]}
    h.pipeline(DEPLOY_YAML, agent=fixed_branch("f", "abc123"),
               config=Config.from_dict({"environments": {"eod": eod}}))  # fmt: skip
    run = h.submit("ship")
    ship(h, run)
    assert h.status(run) == "done"
    assert seen == [["AWS_PROFILE"], ["AWS_PROFILE"]]  # deploy, then test
