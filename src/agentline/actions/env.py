"""env.deploy and env.test: commands from `[environments.<env>]` in agentline.toml (decision 5),
never from the target repo."""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentline.actions._util import config_of, env_section, require, run_cmd, sha_worktree, tail
from agentline.actions.shell import CommandFailed
from agentline.config import Config
from agentline.context import StepContext
from agentline.errors import PermanentError
from agentline.registry import ActionCall


def substitute(template: str, values: dict[str, str | None]) -> str:
    """Replace `{branch}`, `{sha}`, `{env}` with shell-quoted values (branch names come from
    agents, so they are untrusted). Other braces are left alone."""
    out = template
    for k, v in values.items():
        token = "{" + k + "}"
        if token in out:
            if v is None:
                raise PermanentError(f"command needs {token} but no value is available")
            out = out.replace(token, shlex.quote(str(v)))
    return out


def _target(call: ActionCall) -> dict[str, Any]:
    """branch/sha/mirror from params, else the `branch` or `deployment` input."""
    src: dict[str, Any] = {}
    for name in ("branch", "deployment"):
        v = call.inputs.get(name)
        if isinstance(v, dict):
            src = v
            break
    branch = call.params.get("branch") or src.get("name") or src.get("branch")
    return {
        "branch": branch,
        "sha": call.params.get("sha") or src.get("sha"),
        "mirror": call.params.get("mirror") or src.get("mirror"),
    }


@contextlib.asynccontextmanager
async def _cwd(
    section: dict[str, Any], config: Config, target: dict[str, Any]
) -> AsyncIterator[Path | None]:
    """Env config `cwd` (relative to agentline.toml), else a temp worktree of the sha from
    the mirror, else the current directory."""
    if section.get("cwd"):
        cwd = Path(str(section["cwd"])).expanduser()
        if not cwd.is_absolute() and config.path is not None:
            cwd = config.path.parent / cwd
        yield cwd
    elif target.get("mirror") and target.get("sha"):
        async with sha_worktree(target["mirror"], str(target["sha"])) as wt:
            yield wt
    else:
        yield None


@dataclass
class EnvDeploy:
    """Deploy `{sha}` of `{branch}` to an environment. **Deploy-class** (must be gated;
    `on_unknown: ai` is rejected).

    Runs `[environments.<env>].deploy` with `{branch}` / `{sha}` / `{env}` substituted
    (shell-quoted). cwd: env `cwd`, else a temp worktree of the sha from the mirror.

    Effect strategy: **check** if the env has `check = "<cmd>"`, which must print the
    deployed sha; it matches when it equals the input sha (either may be abbreviated).
    Without a check: **none**, so a crash mid-deploy is unknown → `on_unknown` (default
    `halt`, never `ai`). The effect is named `deploy#<attempt>`, so a deploy that finished
    with a non-zero exit is recorded as such and a retry attempt deploys again.

    Params: `env` (required), `timeout`. Returns `{env, sha, branch, exit_code, at,
    output_tail}`; a non-zero exit raises.
    """

    config: Config | None = None
    name: str = "env.deploy"
    deploy_class: bool = True

    async def __call__(self, ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        config = config_of(call, self.config)
        env = str(require(call.params, "env", self.name))
        section = env_section(config, env)
        target = _target(call)
        values = {"branch": target["branch"], "sha": target["sha"], "env": env}
        cmd = substitute(str(config.environment(env).deploy), values)
        check_cmd = substitute(str(section["check"]), values) if section.get("check") else None
        timeout = call.params.get("timeout") or section.get("timeout")
        timeout = float(timeout) if timeout is not None else None
        sha = target["sha"]

        def result(code: int, output: str, **extra: Any) -> dict[str, Any]:
            return {
                "env": env,
                "sha": sha,
                "branch": target["branch"],
                "exit_code": code,
                "at": time.time(),
                "output_tail": tail(output),
                **extra,
            }

        async def check() -> dict[str, Any] | None:
            assert check_cmd is not None
            async with _cwd(section, config, target) as cwd:
                code, out, _ = await run_cmd(check_cmd, cwd=cwd, timeout=timeout or 300)
            deployed = out.strip().splitlines()[-1].strip() if out.strip() else ""
            if code != 0 or not deployed or not sha:
                return None
            if deployed.startswith(str(sha)) or str(sha).startswith(deployed):
                return result(0, "", verified_by="check")
            return None

        async def do() -> dict[str, Any]:
            async with _cwd(section, config, target) as cwd:
                code, out, err = await run_cmd(cmd, cwd=cwd, timeout=timeout)
            return result(code, out + err)

        ctx.notes["deploy"] = {"env": env, "sha": sha, "branch": target["branch"]}
        res = await ctx.effect(
            f"deploy#{ctx.attempt}",
            do,
            check=check if check_cmd else None,
            key=call.effect.key,
            on_unknown=call.effect.on_unknown or "halt",
            deploy_class=True,
            description=f"deploy {sha} ({target['branch']}) to {env}",
        )
        await _audit_deploy(ctx, res, f"deploy#{ctx.attempt}")
        if res["exit_code"] != 0:
            raise CommandFailed(
                f"deploy to {env} exited with {res['exit_code']}: {res['output_tail'][-500:]}",
                res,
            )
        return res


def gate_approver(ctx: StepContext) -> dict[str, Any] | None:
    """`{by, gate, at, comment}` of the run's most recently approved gate, or None."""
    app = ctx.app
    with app.ledger.tx() as tx:
        gates = [
            x
            for x in tx.list_executions(ctx.run_id)
            if x.status == "done" and (x.detail or {}).get("kind") == "gate"
        ]
        if not gates:
            return None
        last = max(gates, key=lambda x: (x.finished_at or 0, x.iteration))
        result = app.scheduler.read_result(tx, last)
    if not isinstance(result, dict):
        return {"by": None, "gate": last.key}
    return {
        "by": result.get("by"),
        "gate": last.key,
        "at": result.get("at"),
        "comment": result.get("comment") or "",
    }


async def _audit_deploy(ctx: StepContext, res: dict[str, Any], effect: str) -> None:
    """One `deploy` audit row per deploy effect: env, sha, branch, approver, exit code.
    Skipped when this effect was already audited (a re-run reusing the committed effect)."""

    def _seen() -> bool:
        with ctx.app.ledger.tx() as tx:
            return any(
                a.action == "deploy"
                and a.node_id == ctx.node_id
                and a.detail.get("effect") == effect
                and a.detail.get("iteration") == ctx.iteration
                for a in tx.list_audit(ctx.run_id)
            )

    if await asyncio.to_thread(_seen):
        return
    approval = await asyncio.to_thread(gate_approver, ctx)
    await ctx.audit(
        "deploy",
        {
            "env": res.get("env"),
            "sha": res.get("sha"),
            "branch": res.get("branch"),
            "exit_code": res.get("exit_code"),
            "approved_by": approval.get("by") if approval else None,
            "gate": approval.get("gate") if approval else None,
            "effect": effect,
            "iteration": ctx.iteration,
            "verified_by": res.get("verified_by"),
        },
    )


@dataclass
class EnvTest:
    """Run `[environments.<env>].test` (same substitution and cwd rules as env.deploy).

    No effect guard: tests against an environment are safe to repeat. A failing test run is
    a result, not an error: `{env, passed: false, exit_code, output_tail}`.
    """

    config: Config | None = None
    name: str = "env.test"
    deploy_class: bool = False

    async def __call__(self, ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        config = config_of(call, self.config)
        env = str(require(call.params, "env", self.name))
        section = env_section(config, env)
        test = config.environment(env).test
        if not test:
            raise PermanentError(f"[environments.{env}] has no `test` command")
        target = _target(call)
        cmd = substitute(test, {"branch": target["branch"], "sha": target["sha"], "env": env})
        timeout = call.params.get("timeout") or section.get("test_timeout")
        async with _cwd(section, config, target) as cwd:
            code, out, err = await run_cmd(
                cmd, cwd=cwd, timeout=float(timeout) if timeout is not None else None
            )
        return {
            "env": env,
            "sha": target["sha"],
            "passed": code == 0,
            "exit_code": code,
            "output_tail": tail(out + err),
        }
