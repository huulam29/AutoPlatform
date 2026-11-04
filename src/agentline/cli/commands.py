"""One function per CLI command: `(env, args) -> exit code`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from agentline.app import App
from agentline.cli import proposal as props
from agentline.cli import view
from agentline.cli.env import CliEnv
from agentline.errors import ValidationError
from agentline.incidents import asyncio_handler, install_process_hooks
from agentline.ledger.base import Execution
from agentline.pipeline import Pipeline, SpecError, load_spec, validate_spec
from agentline.poller import run_poller
from agentline.registry import Registry
from agentline.schedules import next_fire_times, run_schedules, utc_now
from agentline.sources.jira_poll import run_jira_poll
from agentline.states import TERMINAL_RUN, RunStatus

PARKED = frozenset({RunStatus.WAITING, RunStatus.NEEDS_REVIEW})


def _json_arg(text: str | None, what: str) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValidationError(f"{what} is not valid JSON: {e}") from None


def _poll_interval(env: CliEnv) -> float:
    return float((env.config.raw.get("poller") or {}).get("interval", 30.0))


# -- validate ---------------------------------------------------------------------------------
def cmd_validate(env: CliEnv, args: argparse.Namespace) -> int:
    bad = 0
    for f in args.files:
        try:
            spec = load_spec(f)
            issues = validate_spec(spec, env.registry, env.config)
        except SpecError as e:
            issues = e.issues
        except OSError as e:
            env.out(f"{f}: {e}")
            bad += 1
            continue
        for issue in issues:
            env.out(str(issue))
        if issues:
            bad += 1
        elif not args.quiet:
            env.out(f"{f}: ok ({spec.name}, {len(spec.nodes)} nodes)")
    return 1 if bad else 0


# -- run, worker, serve -------------------------------------------------------------------------
def _pipeline_for(env: CliEnv, target: str) -> Pipeline | str:
    p = Path(target)
    if p.suffix in (".yaml", ".yml") or p.is_file():
        return env.load_pipeline_file(p)
    env.app.graph(target)  # raises with "unknown flow" if it isn't loaded
    return target


def cmd_run(env: CliEnv, args: argparse.Namespace) -> int:
    target = _pipeline_for(env, args.pipeline)
    name = target if isinstance(target, str) else target.name
    if args.payload_file:
        payload = _json_arg(Path(args.payload_file).read_text(encoding="utf-8"), "--payload-file")
    else:
        payload = _json_arg(args.payload, "--payload") if args.payload else {}
    sub = env.app.submit_sync(name, payload, args.dedupe_key)
    env.out(
        f"run {sub.run_id} submitted ({name})"
        if sub.created
        else f"run {sub.run_id} already exists for dedupe key {args.dedupe_key!r}"
    )
    if not args.wait:
        return 0
    status = asyncio.run(_wait(env, sub.run_id, args.timeout, args.concurrency, args.lease_ttl))
    run = env.app.get_run(sub.run_id)
    assert run is not None
    if status is None:
        env.out(f"run {sub.run_id}: still {run.status} after {args.timeout:g}s")
        return 2
    env.out(f"run {sub.run_id}: {status}")
    if run.error:
        env.out(f"  error: {view.clip(run.error, 300)}")
    for line in view.next_steps(run, env.app.executions(sub.run_id)):
        env.out(f"  next: {line}")
    return 1 if status in (RunStatus.DEAD, RunStatus.CANCELLED) else 0


async def _wait(
    env: CliEnv, run_id: str, timeout: float | None, concurrency: int, lease_ttl: float
) -> str | None:
    """Run an in-process worker (and poller) until the run is terminal or parked."""
    app = env.app
    stop = asyncio.Event()
    worker = app.worker(concurrency=concurrency, lease_ttl=lease_ttl, poll_interval=0.05)
    tasks = [
        asyncio.create_task(worker.run(stop)),
        asyncio.create_task(run_poller(app, env.registry, stop, _poll_interval(env))),
    ]
    deadline = None if timeout is None else time.monotonic() + timeout
    status: str | None = None
    try:
        while True:
            run = await asyncio.to_thread(app.get_run, run_id)
            assert run is not None
            if run.status in TERMINAL_RUN or run.status in PARKED:
                status = run.status
                break
            if deadline is not None and time.monotonic() > deadline:
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    return status


@contextlib.contextmanager
def _signals(stop: asyncio.Event, extra: Callable[[], None] | None = None) -> Iterator[None]:
    """SIGINT/SIGTERM set `stop` (and call `extra`) for the duration; then restore."""
    loop = asyncio.get_running_loop()

    def handler(*_: Any) -> None:
        loop.call_soon_threadsafe(stop.set)
        if extra is not None:
            extra()

    previous: dict[int, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, RuntimeError):  # not the main thread
            previous[sig] = signal.signal(sig, handler)
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


async def _watch_parent(stop: asyncio.Event, interval: float = 1.0) -> None:
    """With AGENTLINE_PARENT_PID (set by the Mac app), stop when that process is gone, so a
    crashed or force-quit app never leaves an engine running on its own."""
    raw = os.environ.get("AGENTLINE_PARENT_PID", "")
    if not raw.isdigit():
        return
    parent = int(raw)
    while not stop.is_set():
        try:
            os.kill(parent, 0)
            alive = os.getppid() == parent or os.getppid() != 1
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True  # exists, owned by someone else (pid reuse is caught by getppid)
        if not alive:
            logging.getLogger("agentline.cli").warning(
                "parent process %d is gone; stopping the engine", parent
            )
            stop.set()
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval)


def _report_crashes(env: CliEnv) -> None:
    """Long-running commands record unhandled exceptions as incidents (agentline.incidents)."""
    if env.app.incidents is not None:
        install_process_hooks(env.app.incidents)


async def _work(
    env: CliEnv,
    args: argparse.Namespace,
    stop: asyncio.Event,
    chat: bool = False,
    server: Any = None,
) -> None:
    app = env.app
    if app.incidents is not None:
        asyncio.get_running_loop().set_exception_handler(asyncio_handler(app.incidents))
    worker = app.worker(concurrency=args.concurrency, lease_ttl=args.lease_ttl)
    if getattr(args, "until_idle", False):
        await worker.run_until_idle()
        return
    await asyncio.gather(
        _watch_parent(stop),
        worker.run(stop),
        run_poller(app, env.registry, stop, _poll_interval(env)),
        env.live.watch(stop),  # settings and pipeline changes, no restart needed
        run_schedules(app, env.live, stop),
        run_jira_poll(app, env.live, stop),
        *([_chat_loop(env, stop)] if chat else []),
        *([_triage_loop(env, stop, server)] if server is not None else []),
        *([_review_loop(env, stop, server)] if server is not None else []),
        *([_runchat_loop(stop, server)] if server is not None else []),
    )


async def _runchat_loop(stop: asyncio.Event, server: Any) -> None:
    """Chat start nodes during their runs: notes, answers, cards (agentline.runchat).
    AGENTLINE_RUNCHAT=off turns it off (test harnesses: an answer would call a real agent)."""
    from agentline.runchat import enabled

    if enabled():
        await server.state.runchat.run(stop)


async def _review_loop(env: CliEnv, stop: asyncio.Event, server: Any) -> None:
    """Run reviews, probes and follow-up of adopted edits (agentline.review).
    AGENTLINE_REVIEW=off turns it off (test harnesses: a review would call a real agent)."""
    from agentline.review import auto_enabled, run_review_loop

    if not auto_enabled():
        return
    await run_review_loop(env.app, server.state.pipeline_files, stop)


async def _triage_loop(env: CliEnv, stop: asyncio.Event, server: Any) -> None:
    """Automatic failure triage (agentline.triage), with the server's pipelines and issues.
    AGENTLINE_TRIAGE=off turns it off (test harnesses: triage would call a real agent)."""
    from agentline.triage import auto_enabled, run_triage_loop

    if not auto_enabled():
        return

    await run_triage_loop(
        env.app,
        server.state.pipeline_files,
        stop,
        issues=server.state.issues,
        incidents=server.state.incidents,
    )


async def _chat_loop(env: CliEnv, stop: asyncio.Event) -> None:
    """Telegram, Slack and Discord in and out (agentline.chatlink); each idles until its keys
    are set."""
    from agentline.chatlink.platforms import PLATFORMS
    from agentline.chatlink.service import run_chatlink

    await asyncio.gather(
        *(
            run_chatlink(
                env.app,
                stop,
                registry=env.registry,
                pipelines_dir=env.pipelines_dir,
                config_path=env.config_path,
                platform=name,
                transport=p.connect,
            )
            for name, p in PLATFORMS.items()
        )
    )


def cmd_worker(env: CliEnv, args: argparse.Namespace) -> int:
    _report_crashes(env)

    async def main() -> None:
        stop = asyncio.Event()
        env.err(
            f"agentline worker: {len(env.pipelines)} pipeline(s), concurrency"
            f" {args.concurrency}, db {env.db_path}"
        )
        _announce_schedules(env, args)
        with _signals(stop):
            await _work(env, args, stop)

    asyncio.run(main())
    return 0


def _announce_schedules(env: CliEnv, args: argparse.Namespace) -> None:
    if getattr(args, "until_idle", False):
        return
    for row in next_fire_times(env.pipelines, utc_now()):
        env.err(f"  schedule {row['pipeline']}: {row['schedule']} next {row['next'][0]}")


def _build_asgi_app(app: App, pipelines: Any, registry: Registry, env: CliEnv) -> Any:
    """The ASGI app `serve` runs: webhooks + editor + runs API + the built web UI.
    Keep this the only place that builds it."""
    from agentline.api.server import create_server

    return create_server(
        app,
        pipelines,
        pipelines_dir=env.pipelines_dir,
        registry=registry,
        config=env.config,
    )


def cmd_serve(env: CliEnv, args: argparse.Namespace) -> int:
    _report_crashes(env)
    try:
        import uvicorn
    except ImportError:
        env.err(
            "agentline serve needs the optional server dependencies (fastapi, uvicorn):"
            " pip install 'agentline[server]'"
        )
        return 1
    try:
        from agentline.api.server import TOKEN_ENV, ensure_api_token

        if ensure_api_token(args.host):
            from agentline.secrets import secrets_path

            env.err(
                f"serve: {args.host} is reachable from other machines, so the API needs a token;"
                f" created {TOKEN_ENV} in {secrets_path()}. The web UI asks for it once."
            )
        asgi = _build_asgi_app(env.app, env.live, env.registry, env)
    except ImportError as e:
        env.err(f"agentline serve needs the server extra ({e}): pip install 'agentline[server]'")
        return 1
    from agentline import advisor

    if advisor.enabled() and env.app.advisor is None:  # decisions 3 and 78
        env.app.advisor = advisor.make_advisor(asgi.state.pipeline_files)

    async def main() -> None:
        stop = asyncio.Event()
        config = uvicorn.Config(
            asgi, host=args.host, port=args.port, log_config=None, lifespan="off"
        )
        server = uvicorn.Server(config)

        def exit_server() -> None:
            server.should_exit = True

        _announce_schedules(env, args)
        # uvicorn re-raises captured signals after serve() returns; ours just set `stop`
        with _signals(stop, exit_server):
            work = asyncio.create_task(_work(env, args, stop, chat=True, server=asgi))

            async def exit_when_stopped() -> None:  # e.g. the parent-process watchdog
                await stop.wait()
                server.should_exit = True

            stopper = asyncio.create_task(exit_when_stopped())
            try:
                await server.serve()
            finally:
                stop.set()
                await work
                stopper.cancel()

    asyncio.run(main())
    return 0


# -- runs -------------------------------------------------------------------------------------
def cmd_runs_list(env: CliEnv, args: argparse.Namespace) -> int:
    with env.app.ledger.tx() as tx:
        runs = tx.list_runs(args.status, args.limit)
    if args.json:
        env.out_json([view.run_dict(r) for r in runs])
    else:
        env.out(view.render_runs(runs))
    return 0


def cmd_runs_show(env: CliEnv, args: argparse.Namespace) -> int:
    v = view.run_view(env.app, args.run)
    if args.json:
        env.out_json(v)
    else:
        env.out(view.render_run(v))
    return 0


def cmd_runs_retry(env: CliEnv, args: argparse.Namespace) -> int:
    node = getattr(args, "node", None)
    n = asyncio.run(env.app.retry(args.run, node_id=node, actor=env.actor))
    what = f"execution(s) of {node}" if node else "dead execution(s)"
    env.out(f"run {args.run}: {n} {what} back to ready")
    return 0


def cmd_runs_cancel(env: CliEnv, args: argparse.Namespace) -> int:
    asyncio.run(env.app.cancel(args.run, actor=env.actor))
    env.out(f"run {args.run}: cancelled")
    return 0


# -- human decisions ------------------------------------------------------------------------
def cmd_approve(env: CliEnv, args: argparse.Namespace) -> int:
    if args.proposal:
        return _approve_proposal(env, args)
    gate = asyncio.run(
        env.app.approve(args.run, args.node, actor=env.actor, comment=args.comment or "")
    )
    env.out(f"approved {gate.key} of run {args.run} as {env.actor}")
    return 0


def cmd_reject(env: CliEnv, args: argparse.Namespace) -> int:
    gate = asyncio.run(
        env.app.reject(args.run, args.node, actor=env.actor, comment=args.comment or "")
    )
    env.out(f"rejected {gate.key} of run {args.run} as {env.actor}")
    return 0


def _needs_review(env: CliEnv, run_id: str, node: str | None) -> Execution:
    if env.app.get_run(run_id) is None:
        raise ValidationError(f"no run {run_id}")
    found = [
        x
        for x in env.app.executions(run_id)
        if x.status == "needs-review" and node in (None, x.node_id)
    ]
    if not found:
        where = f" at node {node}" if node else ""
        raise ValidationError(f"run {run_id} has no execution in needs-review{where}")
    if len(found) > 1:
        names = ", ".join(x.key for x in found)
        raise ValidationError(
            f"run {run_id} has several executions in needs-review ({names}); pick one with --node"
        )
    return found[0]


def cmd_resolve(env: CliEnv, args: argparse.Namespace) -> int:
    ex = _needs_review(env, args.run, args.node)
    if args.effect_result is not None and args.effect != "done":
        raise ValidationError("--effect-result goes with --effect done")
    asyncio.run(
        env.app.resolve(
            ex.id,
            args.action,
            actor=env.actor,
            result=_json_arg(args.result, "--result"),
            effect_decision=args.effect,
            effect_result=_json_arg(args.effect_result, "--effect-result"),
        )
    )
    env.out(f"resolved {ex.key} of run {args.run}: {args.action}")
    return 0


def _approve_proposal(env: CliEnv, args: argparse.Namespace) -> int:
    app, config = env.app, env.config
    execs = app.executions(args.run)
    if not execs and app.get_run(args.run) is None:
        raise ValidationError(f"no run {args.run}")
    ex, proposal = props.find_proposal(execs, args.proposal)
    if args.node and args.node != ex.node_id:
        raise ValidationError(f"proposal {args.proposal} is for {ex.node_id}, not {args.node}")
    path = env.config_path
    applied = props.apply_proposal(config, path, proposal, args.proposal)
    app.audit(
        "approve_proposal",
        {
            "proposal": args.proposal,
            "execution_id": ex.id,
            "node": ex.key,
            "comment": args.comment or "",
            "config": str(applied.path),
            "backup": str(applied.backup) if applied.backup else None,
            "profile": applied.profile,
            "add_apt": proposal.get("add_apt"),
            "diff": applied.diff,
        },
        actor=env.actor,
        run_id=args.run,
        node_id=ex.node_id,
    )
    env.out(
        f"wrote [images.{applied.profile}] to {applied.path}"
        + (f" (backup: {applied.backup})" if applied.backup else " (new file)")
    )
    env.out(applied.diff.rstrip())

    new_config = type(config).load(applied.path)
    markers = list(((ex.detail or {}).get("image") or {}).get("markers") or [])
    choice = props.image_choice(new_config, applied.profile, props.repo_of(ex, new_config), markers)
    build: dict[str, Any]
    if choice is None:
        build = {"status": "skipped", "reason": "repo unknown; built on demand by the worker"}
    else:
        build = asyncio.run(_build(choice, new_config))
    if build["status"] == "unavailable":
        env.out(
            f"image build unavailable ({build['error']}); the worker builds"
            f" {build.get('tag')} on demand when {ex.key} retries"
        )
    elif build["status"] == "failed":
        env.out(f"image build failed: {build['error']}")
    elif build["status"] == "skipped":
        env.out(f"image build skipped: {build['reason']}")
    else:
        env.out(f"image {build['tag']} {'built' if build.get('built') else 'already present'}")
    asyncio.run(
        app.resolve(
            ex.id,
            "retry",
            actor=env.actor,
            audit_detail={"proposal": args.proposal, "build": build},
        )
    )
    env.out(
        f"{ex.key} of run {args.run} queued for retry"
        " (restart running workers so they load the new agentline.toml)"
    )
    return 0


async def _build(choice: Any, config: Any) -> dict[str, Any]:
    import os
    import shutil

    from agentline.workspaces.images import build_image

    docker = str(
        (config.raw.get("docker") or {}).get("bin")
        or os.environ.get("AGENTLINE_DOCKER")
        or "docker"
    )
    if shutil.which(docker) is None:
        return {"status": "unavailable", "tag": choice.ref, "error": f"{docker} not found"}
    try:
        res = await build_image(choice, docker=docker)
    except (OSError, RuntimeError) as e:
        msg = str(e)
        unavailable = any(
            s in msg.lower()
            for s in ("cannot connect", "daemon", "is the docker daemon running", "not found")
        )
        return {
            "status": "unavailable" if unavailable else "failed",
            "tag": choice.ref,
            "error": msg[-500:],
        }
    return {"status": "ok", **res}


# -- mirror, audit ----------------------------------------------------------------------------
def cmd_mirror(env: CliEnv, args: argparse.Namespace) -> int:
    from agentline.workspaces.git import ensure_mirror

    repo = env.config.repos.get(args.repo)
    if repo is None:
        known = ", ".join(sorted(env.config.repos)) or "none configured"
        raise ValidationError(f"unknown repo {args.repo!r} (known: {known})")
    path = asyncio.run(ensure_mirror(repo))
    env.out(f"mirror {args.repo}: {path}")
    return 0


def cmd_audit(env: CliEnv, args: argparse.Namespace) -> int:
    entries = env.app.audit_log(args.run, args.limit)
    if args.json:
        env.out_json([view.audit_dict(a) for a in entries])
    elif not entries:
        env.out("no audit entries")
    else:
        for a in entries:
            env.out(view.render_audit_line(view.audit_dict(a)))
    return 0


# -- import / export --------------------------------------------------------------------------
def cmd_export(env: CliEnv, args: argparse.Namespace) -> int:
    from agentline.bundle import export_bundle

    root = env.pipelines_dir
    path = (root / f"{args.name.removesuffix('.yaml')}.yaml").resolve()
    if not path.is_relative_to(root.resolve()) or not path.exists():
        env.err(f"no pipeline {args.name!r} in {root}")
        return 1
    data, filename = export_bundle(root, path, env.config)
    out = Path(args.output or filename)
    out.write_bytes(data)
    env.out(f"exported {args.name} to {out}")
    return 0


def cmd_import(env: CliEnv, args: argparse.Namespace) -> int:
    from agentline.bundle import LocalFiles, import_pipeline

    files = LocalFiles(env.pipelines_dir, env.registry, env.config)
    try:
        out = import_pipeline(
            files,
            Path(args.file).read_bytes(),
            folder=args.folder,
            overwrite=args.force,
            apply_settings=args.with_settings,
        )
    except FileExistsError as e:
        env.err(f"{e.args[0]!r} already exists in {files.root} (use --folder or --force)")
        return 1
    except (ValidationError, OSError) as e:
        env.err(str(e))
        return 1
    env.out(f"imported {out['pipeline']} as {out['name']}")
    for a in out["applied"]:
        env.out(f"  added setting {a}")
    for kind, names in out["missing"].items():
        if names:
            env.out(f"  missing {kind}: {', '.join(names)}")
    for i in out["issues"]:
        env.out(f"  issue: {i.get('path')}: {i.get('message')}")
    return 0


# -- skills -----------------------------------------------------------------------------------
def cmd_skills(env: CliEnv, args: argparse.Namespace) -> int:
    """`skills list | fetch | show NAME | search WORDS | add SOURCE | update [NAME] |
    remove NAME` (agentline.skills, agentline.skill_sources)."""
    from agentline import skill_sources
    from agentline.skills import SkillLibrary

    lib = SkillLibrary()
    if args.action == "search":
        hits = skill_sources.search(args.name or "")
        if args.json:
            env.out_json({"skills": hits})
        for h in [] if args.json else hits:
            env.out(f"{h['source']}@{h['skill']:<40} {h['installs']:>8} installs")
        return 0
    if args.action == "add":
        r = skill_sources.install(args.name or "", args.skill, lib)
        env.out(f"installed from {r['source']}: {', '.join(r['installed'])}")
        return 0
    if args.action == "update":
        r = skill_sources.update(lib, [args.name] if args.name else None)
        env.out(f"updated: {', '.join(r['updated']) or 'none'}")
        if r["missing"]:
            env.err(f"no longer in their source: {', '.join(r['missing'])}")
        return 0
    if args.action == "remove":
        if not skill_sources.remove(lib, args.name or ""):
            env.err(f"{args.name!r} isn't an installed skill")
            return 1
        env.out(f"removed {args.name}")
        return 0
    if args.action == "fetch":
        info = lib.fetch()
        env.out(f"fetched {info['count']} skills from {lib.repo} into {lib.dir}")
        return 0
    if args.action == "show":
        skill = lib.get(args.name or "")
        if skill is None:
            env.err(f"no skill {args.name!r}" + ("" if lib.fetched else " (run: skills fetch)"))
            return 1
        env.out(skill.body)
        return 0
    items = lib.list()
    if args.json:
        env.out_json(
            {
                **lib.info(),
                "skills": [
                    {"name": s.name, "description": s.description, "source": s.source}
                    for s in items
                ],
            }
        )
        return 0
    if not lib.fetched:
        env.out(f"the skill library isn't fetched yet: agentline skills fetch ({lib.repo})")
        return 0
    for s in items:
        env.out(f"{s.name:<36} {s.description[:100]}")
    return 0


# -- issues -----------------------------------------------------------------------------------
def cmd_issues(env: CliEnv, args: argparse.Namespace) -> int:
    """`issues list [--all] | show N | set N STATUS [--note ..] | note N TEXT` (agentline.issues).
    `list` first syncs: every incident and dead / needs-review run gets an issue."""
    from dataclasses import asdict
    from datetime import datetime

    from agentline.incidents import IncidentStore
    from agentline.issues import FAILED_RUN_STATUSES, IssueStore

    incidents = env.app.incidents or IncidentStore(redactor=env.redactor)
    store = IssueStore(incidents.root.parent / "issues")

    def when(t: float | None) -> str:
        return datetime.fromtimestamp(t or 0).strftime("%Y-%m-%d %H:%M")

    if args.action == "list":
        runs = []
        with env.app.ledger.tx() as tx:
            for s in FAILED_RUN_STATUSES:
                runs += [asdict(r) for r in tx.list_runs(s, 500)]
        store.sync(incidents.list(1000), runs)
        items = store.list(None if args.all else "active")
        if args.json:
            env.out_json({"dir": str(store.root), "issues": items})
            return 0
        if not items:
            env.out("no open issues" + ("" if args.all else " (--all shows closed ones)"))
            return 0
        for i in items:
            src = i["source"]
            env.out(
                f"#{i['number']:<4} {i['status']:<11} {when(i.get('updated_at'))}  "
                f"{src['kind']}:{src['ref']}  {i.get('title', '')}"
            )
        return 0
    if args.number is None:
        env.err(f"usage: agentline issues {args.action} N")
        return 2
    try:
        if args.action == "set":
            if not args.value:
                env.err("usage: agentline issues set N open|in_progress|fixed|wont_fix")
                return 2
            rec = store.update(args.number, status=args.value, note=args.note)
            env.out(f"#{args.number} is {rec['status']}")
            return 0
        if args.action == "note":
            store.update(args.number, note=args.value or "")
            env.out(f"noted on #{args.number}")
            return 0
    except KeyError:
        env.err(f"no issue #{args.number}")
        return 1
    except ValidationError as e:
        env.err(str(e))
        return 2
    rec = store.get(args.number)
    if rec is None:
        env.err(f"no issue #{args.number}")
        return 1
    if args.json:
        env.out_json(rec)
        return 0
    src = rec["source"]
    env.out(f"#{rec['number']} {rec['title']}\n")
    env.out(
        f"status {rec['status']}  {src['kind']}:{src['ref']}  x{rec.get('occurrences', 1)}  "
        f"created {when(rec['created_at'])}  updated {when(rec['updated_at'])}"
    )
    if rec.get("detail"):
        env.out("\n" + rec["detail"])
    for h in rec.get("history") or []:
        env.out(f"{when(h['at'])}  {h.get('from') or '-'} -> {h['to']}  {h.get('note') or ''}")
    for n in rec.get("notes") or []:
        env.out(f"{when(n['at'])}  note: {n['text']}")
    return 0


# -- run reviews ------------------------------------------------------------------------------
def cmd_review(env: CliEnv, args: argparse.Namespace) -> int:
    """`review list [--run R] [--pipeline P] | show ID | run RUN` (agentline.review; automatic
    reviews run in `serve`). `run` calls the Chat's harness and counts against the run's budget."""
    from datetime import datetime

    from agentline import review
    from agentline.assistant import title

    store = review.ReviewStore()
    if args.action == "list":
        items = store.list(args.run, args.pipeline)
        if args.json:
            env.out_json({"dir": str(store.root), "reviews": items})
            return 0
        if not items:
            env.out(f"no reviews in {store.root}")
        for r in items:
            when = datetime.fromtimestamp(r.get("created_at") or 0).strftime("%Y-%m-%d %H:%M")
            env.out(
                f"{r['id']}  {when}  run {r['run']} of {r['flow']}: {r.get('status')}"
                f"  {r.get('summary') or r.get('error') or ''}"
            )
        return 0
    if args.action == "run":
        if not args.target:
            env.err("review run needs a run id")
            return 2
        from agentline.api.editor import PipelineFiles

        files = PipelineFiles(env.pipelines_dir, env.registry, env.config)
        try:
            rec = asyncio.run(
                review.review(env.app, files, args.target, store=store, manual=True, force=True)
            )
        except KeyError:
            env.err(f"no run {args.target}")
            return 1
    else:
        got = store.get(args.target or "")
        if got is None:
            env.err(f"no review {args.target} in {store.root}")
            return 1
        rec = got
    if args.json:
        env.out_json(rec)
        return 0
    env.out(f"{rec['id']}: run {rec['run']} of {rec['flow']}: {rec.get('status')}")
    if rec.get("summary") or rec.get("error"):
        env.out(f"  {rec.get('summary') or rec.get('error')}")
    for s in review.signal_lines(rec["signals"]) if rec.get("signals") else []:
        env.out(f"signal: {s}")
    for row in rec.get("rows") or []:
        env.out(f"{row['verdict']}: {row['node']} {row['component']}: {row['observed']}")
    if rec.get("lesson"):
        env.out(f"lesson: {rec['lesson']['cue']}: {rec['lesson']['lesson']}")
    for i, a in enumerate(rec.get("actions") or []):
        bad = f"  (issues: {'; '.join(a['issues'])})" if a.get("issues") else ""
        p = (rec.get("probes") or {}).get(str(i)) or {}
        probed = f"  [probed: {p.get('verdict') or p.get('status')}]" if p else ""
        env.out(f"proposal {i}: {title(a)}{bad}{probed}")
    return 0


def cmd_lessons(env: CliEnv, args: argparse.Namespace) -> int:
    """`lessons PIPELINE`: its lesson library and the edits adopted from reviews."""
    from agentline import lessons, review

    name = args.pipeline.removesuffix(".yaml")
    path = (env.pipelines_dir / f"{name}.yaml").resolve()
    if not path.is_file():
        env.err(f"no pipeline {name} in {env.pipelines_dir}")
        return 1
    items = [x.as_dict() for x in lessons.read(path)]
    flow = ""
    with contextlib.suppress(Exception):
        flow = str(load_spec(path).name)
    adoptions = review.ReviewStore().adoptions.list(flow) if flow else []
    if args.json:
        env.out_json({"file": str(lessons.path_for(path)), "lessons": items,
                      "adoptions": adoptions})  # fmt: skip
        return 0
    if not items:
        env.out(f"no lessons yet ({lessons.path_for(path)})")
    for x in items:
        env.out(f"L{x['id']} [{x['status']}] {x['node']} {x['component']}: {x['cue']}")
        env.out(f"    {x['lesson']}  (confidence {x['confidence']})")
    for a in adoptions:
        verdict = a.get("verdict") or f"waiting ({a['after']['runs']} runs so far)"
        env.out(f"edit {a['id']}: {a.get('node')}: {a.get('summary')}: {verdict}"
                + (f" ({a['detail']})" if a.get("detail") else ""))  # fmt: skip
    return 0


# -- triage -----------------------------------------------------------------------------------
def cmd_triage(env: CliEnv, args: argparse.Namespace) -> int:
    """`triage list [--run R] | show ID` (agentline.triage; triage itself runs in `serve`)."""
    from datetime import datetime

    from agentline.assistant import title
    from agentline.triage import TriageStore

    store = TriageStore()
    if args.action == "list":
        items = store.list(args.run)
        if args.json:
            env.out_json({"dir": str(store.root), "triage": items})
            return 0
        if not items:
            env.out(f"no triage in {store.root}")
        for t in items:
            when = datetime.fromtimestamp(t.get("created_at") or 0).strftime("%Y-%m-%d %H:%M")
            env.out(
                f"{t['id']}  {when}  run {t['run']} {t.get('node') or '-'}:"
                f" {t.get('verdict') or t.get('status')}"
                f"  {t.get('summary') or t.get('error') or ''}"
            )
        return 0
    rec = store.get(args.target or "")
    if rec is None:
        env.err(f"no triage {args.target} in {store.root}")
        return 1
    if args.json:
        env.out_json(rec)
        return 0
    env.out(f"{rec['id']}: run {rec['run']} of {rec['flow']}, node {rec.get('node') or '-'}")
    env.out(f"verdict: {rec.get('verdict') or rec.get('status')}  {rec.get('summary') or ''}")
    for line in (rec.get("reasoning") or rec.get("error") or "").splitlines():
        env.out(f"  {line}")
    if rec.get("github"):
        env.out(f"github: {rec['github'].get('url')}")
    for a in rec.get("actions") or []:
        bad = f"  (issues: {'; '.join(a['issues'])})" if a.get("issues") else ""
        env.out(f"proposal: {title(a)}{bad}")
    return 0


# -- incidents --------------------------------------------------------------------------------
def cmd_incidents(env: CliEnv, args: argparse.Namespace) -> int:
    """`incidents list | show ID | seen ID | rm ID | clear | export DIR` (agentline.incidents)."""
    import shutil
    from datetime import datetime

    from agentline.incidents import IncidentStore

    store = env.app.incidents or IncidentStore(redactor=env.redactor)

    def when(t: float | None) -> str:
        return datetime.fromtimestamp(t or 0).strftime("%Y-%m-%d %H:%M")

    if args.action == "list":
        items = store.list(1000)
        if args.json:
            env.out_json({"dir": str(store.root), "incidents": items})
            return 0
        if not items:
            env.out(f"no incidents in {store.root}")
            return 0
        for i in items:
            mark = "*" if i.get("status") == "new" else " "
            env.out(
                f"{mark} {i['id']}  {when(i.get('last_seen'))}  x{i.get('count', 1):<4}"
                f" {i.get('source', '?')}/{i.get('kind', '?')}: {i.get('title', '')}"
            )
        env.out(f"({sum(1 for i in items if i.get('status') == 'new')} new; dir {store.root})")
        return 0
    if args.action == "clear":
        env.out(f"deleted {store.clear()} incident(s)")
        return 0
    if not args.target:
        env.err(
            f"usage: agentline incidents {args.action} {'DIR' if args.action == 'export' else 'ID'}"
        )
        return 2
    if args.action == "export":
        dest = Path(args.target)
        dest.mkdir(parents=True, exist_ok=True)
        items = store.list(100_000)
        for i in items:
            (dest / f"{i['id']}.json").write_text(
                json.dumps(store.get(i["id"]), indent=2), encoding="utf-8"
            )
            att = store.root / i["id"]
            if att.is_dir():
                shutil.copytree(att, dest / i["id"], dirs_exist_ok=True)
        lines = ["# agentline incidents", ""]
        for i in items:
            lines.append(
                f"- **{i.get('title', '')}** ({i.get('source')}/{i.get('kind')}, x{i.get('count')},"
                f" last {when(i.get('last_seen'))}, v{i.get('version')}) `{i['id']}`"
            )
        (dest / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        env.out(f"exported {len(items)} incident(s) to {dest}")
        return 0
    rec = store.get(args.target)
    if rec is None:
        env.err(f"no incident {args.target} in {store.root}")
        return 1
    if args.action == "seen":
        store.mark_seen(args.target)
        env.out(f"marked {args.target} seen")
    elif args.action == "rm":
        store.delete(args.target)
        env.out(f"deleted {args.target}")
    elif args.json:
        env.out_json(rec)
    else:
        env.out(f"{rec['title']}\n")
        env.out(
            f"id {rec['id']}  {rec['source']}/{rec['kind']}  x{rec['count']}  "
            f"first {when(rec['first_seen'])}  last {when(rec['last_seen'])}  "
            f"v{rec['version']}  {rec['platform']}"
        )
        if rec.get("context"):
            env.out(f"context: {json.dumps(rec['context'], default=str)[:2000]}")
        if rec.get("traceback"):
            env.out("\n" + rec["traceback"])
        for a in rec.get("attachments") or []:
            env.out(f"attachment: {store.root / a}")
    return 0


# -- keys -------------------------------------------------------------------------------------
def cmd_keys(env: CliEnv, args: argparse.Namespace) -> int:
    """`keys list | set NAME | rm NAME`: the secrets file (docs/harnesses-and-models.md). Values
    are read from a hidden prompt (or --stdin) and never printed."""
    import getpass

    from agentline import agents as ai
    from agentline.secrets import (
        delete_secret,
        read_secrets,
        secret_status,
        secrets_path,
        set_secret,
    )

    if args.action == "list":
        names = {p.api_key_env for p in ai.providers(env.config).values() if p.api_key_env}
        for k in secret_status(sorted(names | set(read_secrets()))):
            state = f"set ({k['source']})" if k["set"] else "not set"
            env.out(f"{k['name']:<28} {state}")
        env.out(f"secrets file: {secrets_path()}")
        return 0
    if not args.name:
        env.err(f"usage: agentline keys {args.action} NAME")
        return 2
    if args.action == "rm":
        if not delete_secret(args.name):
            env.err(f"{args.name} isn't in {secrets_path()}")
            return 1
        env.out(f"removed {args.name}")
        return 0
    value = sys.stdin.readline().rstrip("\n") if args.stdin else getpass.getpass(f"{args.name}: ")
    set_secret(args.name, value)
    env.out(f"saved {args.name} to {secrets_path()} (restart workers to use it)")
    return 0


# -- schedules, templates -------------------------------------------------------------------
def cmd_schedules(env: CliEnv, args: argparse.Namespace) -> int:
    rows = next_fire_times(env.pipelines, utc_now(), max(1, args.count))
    if args.json:
        env.out_json(rows)
        return 0
    if not rows:
        env.out(f"no pipelines with a schedule trigger in {env.pipelines_dir}")
        return 0
    for r in rows:
        env.out(f"{r['pipeline']}  {r['schedule']}  ({r['timezone']}, catchup: {r['catchup']})")
        for utc, local in zip(r["next"], r["next_local"], strict=True):
            env.out(f"  {utc}" + ("" if r["timezone"] == "UTC" else f"  ({local})"))
    return 0


def cmd_templates(env: CliEnv, args: argparse.Namespace) -> int:
    from agentline.templates import list_templates

    found = list_templates()
    if args.json:
        env.out_json([{"name": t.name, "description": t.description} for t in found])
        return 0
    width = max((len(t.name) for t in found), default=0)
    for t in found:
        env.out(f"{t.name.ljust(width)}  {t.description}")
    return 0


def cmd_new(env: CliEnv, args: argparse.Namespace) -> int:
    from agentline.templates import copy_template

    written = copy_template(args.template, args.dest, name=args.name, force=args.force)
    for path in written:
        env.out(f"wrote {path}")
    pipeline = written[0]
    spec = load_spec(pipeline)
    issues = validate_spec(spec, env.registry, env.config)
    for issue in issues:
        env.out(str(issue))
    if issues:
        return 1
    env.out(f"{pipeline}: ok ({spec.name}, {len(spec.nodes)} nodes)")
    env.out(f"next: edit the 'change me' lines, then agentline validate {pipeline}")
    return 0
