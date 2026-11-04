"""Dry run API (agentline.dryrun).

POST /dryrun  {name, source?, payload?, routes?: {router: [labels]}, events?: {node: payload}}
              -> {status, nodes, routes, calls, journal}; 422 with issues if it doesn't validate
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agentline.dryrun import dry_run
from agentline.errors import ValidationError
from agentline.pipeline.spec import SpecError, parse_spec


class DryRunBody(BaseModel):
    name: str
    source: str | None = None  # unsaved editor text; default: the file
    payload: Any = None
    routes: dict[str, list[str]] = Field(default_factory=dict)
    events: dict[str, Any] = Field(default_factory=dict)


def create_dryrun_router(files: Any) -> APIRouter:
    router = APIRouter()

    @router.post("/dryrun")
    async def post_dryrun(body: DryRunBody) -> dict[str, Any]:
        path = files.resolve(body.name)
        text = (
            body.source
            if body.source is not None
            else (path.read_text(encoding="utf-8") if path.exists() else None)
        )
        if text is None:
            raise HTTPException(404, f"no pipeline {body.name!r}")
        try:
            spec = parse_spec(text, path=path, base_dir=path.parent)
            return await dry_run(
                spec, files.registry, files.config,
                payload=body.payload, routes=body.routes, events=body.events,
            )  # fmt: skip
        except SpecError as e:
            raise HTTPException(
                422,
                {"message": "the pipeline doesn't validate", "issues": [str(i) for i in e.issues]},
            ) from None
        except ValidationError as e:
            raise HTTPException(422, {"message": str(e), "issues": [str(e)]}) from None

    return router
