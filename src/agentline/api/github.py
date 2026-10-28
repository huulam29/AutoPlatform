"""GitHub setup API for failure-triage reports (agentline.github_setup; Settings > General).

GET    /github           {repo, auto, labels, error, token_source, ready, gh: {installed,
                         logged_in, user}, detected: [{repo, remote, protocol, where, url}],
                         login}; never a token
PUT    /github           {repo?, auto?}: write [report] in agentline.toml
POST   /github/login     start the GitHub CLI's browser sign-in: {state, code, url, error}
GET    /github/login     its progress; DELETE /github/login cancels it
POST   /github/test      {ok, repo, private, token_source, error}: can issues be opened there?
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentline import github_setup as gs
from agentline.errors import ValidationError


class GitHubBody(BaseModel):
    repo: str | None = None
    auto: bool | None = None


def create_github_router(files: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/github")
    async def get_status() -> dict[str, Any]:
        return await asyncio.to_thread(gs.status, files.config, files)

    @router.put("/github")
    async def put_setup(body: GitHubBody) -> dict[str, Any]:
        from agentline.api.settings import config_path
        from agentline.config import Config

        path = config_path(files.config)
        try:
            await asyncio.to_thread(gs.save, path, body.repo, body.auto)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        files.config = Config.load(path)
        return await asyncio.to_thread(gs.status, files.config, files)

    @router.post("/github/login")
    async def start_login() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(gs.login.start)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None

    @router.get("/github/login")
    def login_state() -> dict[str, Any]:
        return gs.login.view()

    @router.delete("/github/login")
    def cancel_login() -> dict[str, Any]:
        return gs.login.cancel()

    @router.post("/github/test")
    async def test() -> dict[str, Any]:
        return await asyncio.to_thread(gs.test, files.config)

    return router
