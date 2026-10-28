"""Dev server: `python -m agentline.api.dev --pipelines examples --db /tmp/x.db [--port 8787]`.

Builds an App on the SQLite ledger, the built-in registry (plus `agentline.toml` from the
pipelines dir, else $AGENTLINE_CONFIG / ./agentline.toml), every valid pipeline under
`--pipelines`, and `create_server`, then runs uvicorn bound to 127.0.0.1 only. `--worker` also
runs a worker (and automatic failure triage) in the same process so submitted runs make
progress. Dev only; production is `agentline serve` (Phase 6). Needs the `server` extra
(`uv sync --extra server`).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from agentline import App
from agentline.api.server import create_server
from agentline.api.settings import config_path
from agentline.config import Config
from agentline.ledger import ledger_from_url
from agentline.live import LivePipelines


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m agentline.api.dev")
    ap.add_argument("--pipelines", required=True, help="directory with pipeline *.yaml files")
    ap.add_argument("--db", required=True, help="SQLite ledger path")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--static", default=None, help="built UI directory (default: web/dist)")
    ap.add_argument("--worker", action="store_true", help="also run a worker in this process")
    args = ap.parse_args(argv)

    import uvicorn

    from agentline.logs import setup_logging

    setup_logging("text", "INFO")
    pdir = Path(args.pipelines)
    toml = pdir / "agentline.toml"
    if not toml.exists():  # where Settings writes: $AGENTLINE_CONFIG or ./agentline.toml
        toml = config_path(Config.load())
    from agentline.harness_migration import migrate

    migrate(toml, pdir)  # once per data folder (phase 44)
    app = App(ledger_from_url(str(args.db)))
    # settings and pipeline changes reach the engine without a restart (agentline.live)
    live = LivePipelines(app, pdir, toml)
    for name, problem in live.last.get("problems", {}).items():
        print(f"not registered (fix it in the editor): {name}: {problem}", file=sys.stderr)
    server = create_server(
        app,
        live,
        pipelines_dir=pdir,
        static_dir=args.static,
        registry=live.registry,
        config=live.config,
    )
    if args.worker:
        tasks: list[asyncio.Task[None]] = []
        stop = asyncio.Event()  # the dev server stops with the process

        @server.on_event("startup")
        async def start_worker() -> None:
            from agentline import review
            from agentline.triage import auto_enabled, run_triage_loop

            loop = asyncio.get_running_loop()
            tasks.append(loop.create_task(app.worker().run()))
            tasks.append(loop.create_task(live.watch(stop)))
            if auto_enabled():
                tasks.append(
                    loop.create_task(
                        run_triage_loop(
                            app,
                            server.state.pipeline_files,
                            stop,
                            issues=server.state.issues,
                            incidents=server.state.incidents,
                        )
                    )
                )
            from agentline import runchat

            if runchat.enabled():
                tasks.append(loop.create_task(server.state.runchat.run(stop)))
            if review.auto_enabled():
                tasks.append(
                    loop.create_task(review.run_review_loop(app, server.state.pipeline_files, stop))
                )

    print(f"agentline dev server: http://127.0.0.1:{args.port}/  (pipelines: {pdir.resolve()})")
    uvicorn.run(server, host="127.0.0.1", port=args.port, log_config=None)  # our logging


if __name__ == "__main__":
    main()
