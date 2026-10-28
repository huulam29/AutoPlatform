"""First-run setup API (agentline.setup).

GET  /setup           {ready, first_run, needs_setup, choice, assistant, options, managed}
PUT  /setup/choice    {harness, model?} the Chat's harness; must resolve
POST /setup/dismiss   don't show the popup again while something works
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentline import agents as ai
from agentline import setup
from agentline.errors import ValidationError


class ChoiceBody(BaseModel):
    harness: str | None = None
    agent: str | None = None  # the old name of `harness`
    model: str | None = None


class VisionBody(BaseModel):
    choice: str  # @auto | @claude-code | a [models] name


def create_setup_router(files: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/setup")
    async def get_setup() -> dict[str, Any]:
        return await asyncio.to_thread(setup.status, files.config)

    @router.put("/setup/choice")
    async def put_choice(body: ChoiceBody) -> dict[str, Any]:
        name = body.harness or body.agent or ""
        try:
            preset = ai.HARNESS_PRESETS.get(name)
            if preset is not None and name not in ai.harnesses(files.config):
                # a CLI harness picked in the setup: set it up first (Settings > Harnesses)
                from agentline.assistant import apply_setting

                table = {"kind": preset.kind, "model": preset.model}
                await asyncio.to_thread(
                    apply_setting, files, "harnesses", name, {k: v for k, v in table.items() if v}
                )
            setup.choose(files.config, name, body.model or None)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return await asyncio.to_thread(setup.status, files.config)

    @router.get("/setup/vision")
    async def get_vision() -> dict[str, Any]:
        """Who reads images for Chat harnesses that can't (agentline.vision)."""
        from agentline import vision

        return await asyncio.to_thread(vision.status, files.config)

    @router.put("/setup/vision")
    async def put_vision(body: VisionBody) -> dict[str, Any]:
        from agentline import vision

        try:
            vision.set_choice(body.choice, files.config)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return await asyncio.to_thread(vision.status, files.config)

    @router.post("/setup/dismiss")
    async def post_dismiss() -> dict[str, Any]:
        setup.dismiss()
        return await asyncio.to_thread(setup.status, files.config)

    return router
