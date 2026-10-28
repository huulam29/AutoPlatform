"""Pipeline history API (agentline.history).

GET  /history?name=          commits that touched the pipeline file, newest first
GET  /history/show?name=&commit=   the file at that commit
POST /history/restore {name, commit}   write that version back (itself a new commit)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentline import history


class RestoreBody(BaseModel):
    name: str
    commit: str


def create_history_router(files: Any) -> APIRouter:
    from agentline.api.editor import _write

    router = APIRouter()

    def rel(name: str) -> str:
        return files.resolve(name).relative_to(files.root).as_posix()

    @router.get("/history")
    def get_history(name: str) -> dict[str, Any]:
        return {"available": history.available(), "commits": history.log(files.root, rel(name))}

    @router.get("/history/show")
    def show(name: str, commit: str) -> dict[str, Any]:
        text = history.show(files.root, commit, rel(name))
        if text is None:
            raise HTTPException(404, "no such version")
        return {"commit": commit, "source": text}

    @router.post("/history/restore")
    def restore(body: RestoreBody) -> dict[str, Any]:
        path = files.resolve(body.name)
        text = history.show(files.root, body.commit, rel(body.name))
        if text is None:
            raise HTTPException(404, "no such version")
        with files.lock:
            history.record(files.root, "before restore")
            _write(path, text)
            commit = history.record(
                files.root, f"restore {files.name_of(path)} to {body.commit[:8]}"
            )
        return {"source": text, "commit": commit, "model": files.model(text, path)}

    return router
