"""One FastAPI app for `agentline serve`: webhooks + editor API + run view API + the web UI.

    create_server(app, pipelines, *, pipelines_dir, github_secret=None, jira_secret=None,
                  token=None, static_dir=None, registry=None, config=None) -> FastAPI

- Webhook routes (`/healthz`, `/webhooks/github`, `/webhooks/jira`) come from
  `api.webhooks.create_app` unchanged and keep their own signature/secret auth.
- `/api/*` (editor + runs) requires `Authorization: Bearer <token>` when `token` (default
  `$AGENTLINE_API_TOKEN`) is set; without a token the API is open, so bind to 127.0.0.1.
- The built UI (`web/dist`, or `static_dir`) is served at `/` when it exists.
- `registry` / `config` are used to validate pipelines in the editor (default: the built-in
  registry). Binding is the caller's job (`uvicorn.run(server, host=..., port=...)`).
"""

from __future__ import annotations

import asyncio
import hmac
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agentline.api.assistant import create_assistant_router, create_attachments_router
from agentline.api.bundles import create_bundles_router
from agentline.api.chat import create_chat_router
from agentline.api.costs import create_costs_router
from agentline.api.dryrun import create_dryrun_router
from agentline.api.editor import PipelineFiles, create_editor_router
from agentline.api.feedback import create_feedback_router
from agentline.api.github import create_github_router
from agentline.api.history import create_history_router
from agentline.api.incidents import create_incidents_router
from agentline.api.issues import create_issues_router
from agentline.api.reviews import create_reviews_router
from agentline.api.runs import create_runs_router
from agentline.api.settings import create_settings_router
from agentline.api.setup import create_setup_router
from agentline.api.skills import create_skills_router
from agentline.api.triage import create_triage_router
from agentline.api.webhooks import create_app
from agentline.app import App
from agentline.config import Config
from agentline.incidents import IncidentStore
from agentline.issues import IssueStore
from agentline.registry import Registry

DEFAULT_STATIC = Path(__file__).resolve().parents[3] / "web" / "dist"
TOKEN_ENV = "AGENTLINE_API_TOKEN"


def is_local(host: str) -> bool:
    """Loopback only: `localhost`, 127.0.0.0/8, ::1."""
    import ipaddress

    if host.strip("[]").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False  # a host name: it may resolve to anything


def ensure_api_token(host: str) -> bool:
    """Off localhost the API always needs a bearer token (decision 82): with none set, create
    one in the secrets file (never printed; the web UI asks for it). True when one was made."""
    import secrets as pysecrets

    from agentline.secrets import set_secret

    if is_local(host) or os.environ.get(TOKEN_ENV):
        return False
    set_secret(TOKEN_ENV, pysecrets.token_urlsafe(32))
    return True


def _token_check(token: str | None) -> Any:
    def check(request: Request) -> None:
        if not token:
            return
        header = request.headers.get("authorization", "")
        given = header[7:] if header.lower().startswith("bearer ") else ""
        if not given or not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(
                401, "missing or bad bearer token", headers={"WWW-Authenticate": "Bearer"}
            )

    return check


def create_server(
    app: App,
    pipelines: Sequence[Any],
    *,
    pipelines_dir: str | Path,
    github_secret: str | None = None,
    jira_secret: str | None = None,
    token: str | None = None,
    static_dir: str | Path | None = None,
    registry: Registry | None = None,
    config: Config | None = None,
) -> FastAPI:
    """Build (not bind) the server. `pipelines` are compiled pipelines for webhook triggers,
    or an `agentline.live.LivePipelines` (then saves in the editor and Settings reach the
    running engine without a restart); `pipelines_dir` is where the editor reads and writes
    YAML files."""
    from agentline.live import LivePipelines

    server = create_app(app, pipelines, github_secret=github_secret, jira_secret=jira_secret)
    token = token or os.environ.get(TOKEN_ENV) or None
    files = PipelineFiles(pipelines_dir, registry, config)
    if isinstance(pipelines, LivePipelines):
        files.attach(pipelines)

        @server.middleware("http")
        async def reload_after_writes(request: Request, call_next: Any) -> Any:
            """After a successful write (a save in the editor or Settings, a Chat apply, an
            import, a restore), the running engine picks it up before the next request."""
            response = await call_next(request)
            if request.method in ("POST", "PUT", "DELETE") and response.status_code < 400:
                await asyncio.to_thread(files.changed, f"{request.method} {request.url.path}")
            return response

    auth = [Depends(_token_check(token))]
    server.include_router(create_editor_router(files), prefix="/api", dependencies=auth)
    from agentline.runchat import RunChatLoop

    server.state.runchat = RunChatLoop(app, files)  # run by `serve` (agentline.runchat)
    server.include_router(
        create_runs_router(app, files, server.state.runchat), prefix="/api", dependencies=auth
    )
    server.include_router(create_settings_router(files), prefix="/api", dependencies=auth)
    server.include_router(create_skills_router(), prefix="/api", dependencies=auth)
    server.include_router(create_attachments_router(), prefix="/api")
    server.include_router(create_setup_router(files), prefix="/api", dependencies=auth)
    server.include_router(create_bundles_router(files), prefix="/api", dependencies=auth)
    server.include_router(create_chat_router(), prefix="/api", dependencies=auth)
    server.include_router(create_costs_router(app), prefix="/api", dependencies=auth)
    server.include_router(create_dryrun_router(files), prefix="/api", dependencies=auth)
    server.include_router(create_history_router(files), prefix="/api", dependencies=auth)
    server.include_router(create_github_router(files), prefix="/api", dependencies=auth)
    store = app.incidents if app.incidents is not None else IncidentStore(redactor=app.redactor)
    server.include_router(create_incidents_router(store), prefix="/api", dependencies=auth)
    issues = IssueStore(store.root.parent / "issues")
    server.include_router(
        create_issues_router(app, store, issues, files), prefix="/api", dependencies=auth
    )
    server.include_router(
        create_assistant_router(files, app, issues, store), prefix="/api", dependencies=auth
    )
    server.include_router(
        create_triage_router(app, files, issues, incidents=store),
        prefix="/api",
        dependencies=auth,
    )
    server.include_router(create_reviews_router(app, files), prefix="/api", dependencies=auth)
    server.include_router(create_feedback_router(app, files), prefix="/api", dependencies=auth)

    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        """Any unhandled error in a request becomes an incident; the client gets its id."""
        rec = store.record_exception(
            "api_error", exc, context={"method": request.method, "path": request.url.path}
        )
        return JSONResponse(
            {"detail": "internal error (recorded as an incident)", "incident": rec.get("id")},
            status_code=500,
        )

    server.add_exception_handler(Exception, internal_error)
    server.state.pipeline_files = files
    server.state.issues = issues
    server.state.incidents = store

    env_static = os.environ.get("AGENTLINE_WEB_DIST")
    static = (
        Path(static_dir)
        if static_dir is not None
        else Path(env_static)
        if env_static
        else DEFAULT_STATIC
    )
    if (static / "index.html").is_file():
        server.mount("/", StaticFiles(directory=static, html=True), name="ui")
    return server
