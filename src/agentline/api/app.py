"""What the Mac app's native bridge answers, for a plain browser (spec 006).

GET  /auth/check            {token_required, ok} for the candidate bearer token (no token
                            dependency: the sign-in screen asks it; never returns the token)
GET  /app/info              version, folders, address, mac_app, local, token_required, ...
POST /engine/restart        202, then `serve` restarts in place (same address); local or token
GET  /app/folders?path=     the sub-folders of `path` (default home), names only
PUT  /app/pipelines-dir     {path}: saved to agentline.toml and live in the running engine
POST /app/reveal            {path} or {target}: the system file manager; local requests only

"Local" (`is_local_request`) is a loopback client with no proxy headers. `serve` puts its stop
event, restart flag and address on `server.state`; without them (the dev server) a restart is
refused. Inside the Mac app (`AGENTLINE_PARENT_PID`) restart is refused too: the app does it.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from agentline import reveal as reveal_mod
from agentline.api.settings import config_path
from agentline.app import default_actor
from agentline.runmarker import in_mac_app

log = logging.getLogger("agentline.api.app")
_PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-real-ip")


def is_local_request(request: Request) -> bool:
    from agentline.api.server import is_local

    host = request.client.host if request.client else ""
    if not host or not is_local(host):
        return False
    return not any(h in request.headers for h in _PROXY_HEADERS)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def _home() -> Path:
    return Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline").expanduser()


class FolderBody(BaseModel):
    path: str


class RevealBody(BaseModel):
    path: str | None = None
    target: str | None = None


def create_auth_router(token: str | None) -> APIRouter:
    router = APIRouter()

    @router.get("/auth/check")
    def check(request: Request) -> dict[str, Any]:
        if not token:
            return {"token_required": False, "ok": True}
        given = _bearer(request)
        ok = bool(given) and hmac.compare_digest(given.encode(), token.encode())
        return {"token_required": True, "ok": ok}

    return router


def create_app_router(files: Any, state: Any, token: str | None) -> APIRouter:
    """`files` is the server's PipelineFiles; `state` the FastAPI app's `state`."""
    router = APIRouter()

    def info(request: Request) -> dict[str, Any]:
        from agentline import __version__

        local = is_local_request(request)
        cfg_path = config_path(files.config)
        return {
            "version": __version__,
            "data_folder": str(_home()),
            "config_path": str(cfg_path),
            "pipelines_dir": str(files.root),
            "pipelines_dir_from_cli": bool(getattr(state, "pipelines_from_cli", False)),
            "address": getattr(state, "address", None),
            "mac_app": in_mac_app(),
            "local": local,
            "token_required": bool(token),
            "can_restart": not in_mac_app()
            and getattr(state, "stop", None) is not None
            and (local or bool(token)),
            "can_reveal": local,
        }

    router.get("/app/info")(info)

    @router.post("/engine/restart", status_code=202)
    def restart(request: Request, background: BackgroundTasks) -> dict[str, Any]:
        if in_mac_app():
            raise HTTPException(409, "the Mac app restarts its engine")
        stop = getattr(state, "stop", None)
        if stop is None:
            raise HTTPException(409, "restart needs agentline serve")
        if not (is_local_request(request) or token):
            raise HTTPException(403, "restart is allowed from this machine or with the API token")
        if getattr(state, "restart", False):
            raise HTTPException(409, "a restart is already under way")
        state.restart = True
        client = request.client.host if request.client else "unknown"
        log.warning("engine restart asked by %s from %s", default_actor(), client)
        loop = getattr(state, "loop", None)
        if loop is not None:  # the stop event belongs to serve's event loop
            background.add_task(loop.call_soon_threadsafe, stop.set)
        else:
            background.add_task(stop.set)
        return {"restarting": True}

    @router.get("/app/folders")
    def folders(path: str = "") -> dict[str, Any]:
        home = Path.home()
        p = Path(path).expanduser() if path else home
        if not p.is_absolute():
            p = home / p
        p = p.resolve()
        if not p.is_dir():
            raise HTTPException(400, f"not a folder: {p}")
        out = []
        try:
            entries = sorted(p.iterdir(), key=lambda x: x.name.lower())
        except OSError as e:
            raise HTTPException(400, f"can't read {p}: {e.strerror or e}") from None
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            out.append(
                {"name": e.name, "path": str(e), "readable": os.access(e, os.R_OK | os.X_OK)}
            )
        parent = str(p.parent) if p.parent != p else None
        return {"path": str(p), "parent": parent, "home": str(home), "folders": out}

    @router.put("/app/pipelines-dir")
    def set_pipelines_dir(body: FolderBody) -> dict[str, Any]:
        from agentline import tomlwrite

        raw = body.path.strip()
        if not raw:
            raise HTTPException(400, "give a folder")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            raise HTTPException(400, f"use a full path: {raw}")
        p = p.resolve()
        if not p.exists():
            raise HTTPException(400, f"no such folder: {p}")
        if not p.is_dir():
            raise HTTPException(400, f"not a folder: {p}")
        cfg = config_path(files.config)
        text = cfg.read_text() if cfg.exists() else ""
        try:
            new = tomlwrite.set_top_level(text, "pipelines_dir", str(p))
        except Exception as e:
            raise HTTPException(400, str(e)) from None
        seeded = False
        from agentline.pipeline.files import pipeline_files

        if not pipeline_files(p):
            from agentline.templates import copy_template

            copy_template("ticket-to-eod", p)
            seeded = True
        cfg.parent.mkdir(parents=True, exist_ok=True)
        tomlwrite.write_file(cfg, new)
        files.set_dir(p)
        note = None
        if getattr(state, "pipelines_from_cli", False):
            note = (
                "The engine was started with --pipelines: that folder is used again after a"
                " restart unless the command changes."
            )
        return {"pipelines_dir": str(p), "seeded": seeded, "note": note}

    @router.post("/app/reveal")
    def reveal(request: Request, body: RevealBody) -> dict[str, Any]:
        if not is_local_request(request):
            raise HTTPException(403, "Show in folder works only on the engine's own machine")
        cfg = config_path(files.config)
        targets = {
            "data": _home(),
            "pipelines": files.root,
            "config": cfg,
            "incidents": _home() / "incidents",
        }
        if body.target:
            if body.target not in targets:
                raise HTTPException(400, f"target must be one of {', '.join(targets)}")
            path = Path(targets[body.target])
        elif body.path:
            path = Path(body.path).expanduser()
        else:
            raise HTTPException(400, "give a path or a target")
        roots = [_home(), files.root, cfg.parent]
        if not reveal_mod.allowed(path, roots):
            raise HTTPException(400, "only the data, pipelines and settings folders can be shown")
        if not path.exists():
            raise HTTPException(400, f"{path} doesn't exist")
        try:
            reveal_mod.reveal(path)
        except OSError as e:
            raise HTTPException(400, f"couldn't open the file manager: {e}") from None
        return {"revealed": str(path.resolve())}

    return router
