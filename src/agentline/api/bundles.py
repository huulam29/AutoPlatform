"""Pipeline import / export (agentline.bundle).

GET  /export/{name}                       the pipeline as <folder>.agentline.zip
POST /import?folder=&overwrite=&apply_settings=   body: the .zip
     -> {name, issues, missing, settings, applied}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from agentline import bundle
from agentline.api.settings import saved_note
from agentline.errors import ValidationError


def create_bundles_router(files: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/export/{name:path}")
    def export(name: str) -> Response:
        path = files.resolve(name)
        if not path.exists():
            raise HTTPException(404, f"no pipeline {name!r}")
        try:
            data, filename = bundle.export_bundle(files.root, path, files.config)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return Response(
            data,
            media_type="application/zip",
            headers={"content-disposition": f'attachment; filename="{filename}"'},
        )

    @router.post("/import")
    async def import_(
        request: Request,
        folder: str | None = None,
        overwrite: bool = False,
        apply_settings: bool = False,
    ) -> dict[str, Any]:
        data = await request.body()
        try:
            out = bundle.import_pipeline(
                files, data, folder=folder or None, overwrite=overwrite,
                apply_settings=apply_settings,
            )  # fmt: skip
        except FileExistsError as e:
            raise HTTPException(409, f"{e.args[0]!r} already exists: pick another folder") from None
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return {**out, "note": saved_note(files) if out["applied"] else None}

    return router
