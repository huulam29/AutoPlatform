"""Skills API: the local agent-skill library (agentline.skills).

GET    /skills          {repo, dir, fetched, fetched_at,
                         skills: [{name, description, source, installed_from?}]}
POST   /skills/fetch    download / refresh it from GitHub
GET    /skills/search?q=  the skills.sh marketplace: {skills: [{id, name, skill, source, installs}]}
POST   /skills/preview  {source}: the skills a GitHub source has (agentline.skill_sources)
POST   /skills/install  {source, names?}: install them (all when names is left out)
POST   /skills/update   {names?}: download installed skills again from their sources
GET    /skills/{name}   one skill with its text
PUT    /skills/{name}   {description, body}: create / replace your own (local) skill
DELETE /skills/{name}   delete your own skill, or remove an installed one
"""

from __future__ import annotations

import asyncio
import urllib.error
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentline import skill_sources
from agentline.errors import ValidationError
from agentline.skills import SUGGESTED, SkillLibrary


def install_name(skill: str) -> str:
    return skill_sources.install_name(skill, skill)


class SkillBody(BaseModel):
    description: str = ""
    body: str


class SourceBody(BaseModel):
    source: str
    names: list[str] | None = None


class UpdateBody(BaseModel):
    names: list[str] | None = None


def skills_view(lib: SkillLibrary) -> dict[str, Any]:
    origin = skill_sources.installed(lib)

    def row(s: Any) -> dict[str, Any]:
        out = {"name": s.name, "description": s.description, "source": s.source}
        if s.source == "installed" and s.name in origin:
            e = origin[s.name]
            out["installed_from"] = {k: e.get(k) for k in ("source", "repo", "ref", "path", "sha",
                                                           "installed_at")}  # fmt: skip
        return out

    return {
        **lib.info(),
        "skills": [row(s) for s in lib.list()],
        "local_dir": str(lib.local_dir),
        "suggested": SUGGESTED,
    }


def create_skills_router(library: SkillLibrary | None = None) -> APIRouter:
    router = APIRouter()

    def lib() -> SkillLibrary:
        return library or SkillLibrary()

    @router.get("/skills")
    def get_skills() -> dict[str, Any]:
        return skills_view(lib())

    @router.post("/skills/fetch")
    async def fetch_skills() -> dict[str, Any]:
        try:
            await asyncio.to_thread(lib().fetch)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise HTTPException(502, f"couldn't download {lib().repo}: {e}") from None
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return skills_view(lib())

    async def _remote(fn: Any, *args: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, *args)
        except urllib.error.HTTPError as e:
            what = "not found (is the repo public?)" if e.code == 404 else f"HTTP {e.code}"
            raise HTTPException(502, f"download failed: {what}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise HTTPException(502, f"couldn't reach GitHub / skills.sh: {e}") from None
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None

    @router.get("/skills/search")
    async def search_skills(q: str = "", limit: int = 20) -> dict[str, Any]:
        found = await _remote(skill_sources.search, q, limit)
        here = {s.name: s.source for s in lib().list()}
        origin = skill_sources.installed(lib())
        for f in found:  # installed = this exact skill came from this repo
            e = origin.get(install_name(f["skill"]))
            f["installed"] = bool(e and str(e.get("repo", "")).lower() == f["source"].lower())
            f["exists"] = here.get(install_name(f["skill"]))
        return {"query": q, "skills": found}

    @router.post("/skills/preview")
    async def preview_source(body: SourceBody) -> dict[str, Any]:
        return await _remote(skill_sources.preview, body.source, lib())

    @router.post("/skills/install")
    async def install_skills(body: SourceBody) -> dict[str, Any]:
        result = await _remote(skill_sources.install, body.source, body.names, lib())
        return {**skills_view(lib()), "result": result}

    @router.post("/skills/update")
    async def update_skills(body: UpdateBody) -> dict[str, Any]:
        result = await _remote(skill_sources.update, lib(), body.names)
        return {**skills_view(lib()), "result": result}

    @router.get("/skills/{name}")
    def get_skill(name: str) -> dict[str, Any]:
        skill = lib().get(name)
        if skill is None:
            raise HTTPException(404, f"no skill {name!r}")
        return {"name": skill.name, "description": skill.description, "source": skill.source,
                "body": skill.body}  # fmt: skip

    @router.put("/skills/{name}")
    def put_skill(name: str, body: SkillBody) -> dict[str, Any]:
        try:
            lib().save_local(name, body.description, body.body)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return skills_view(lib())

    @router.delete("/skills/{name}")
    def delete_skill(name: str) -> dict[str, Any]:
        if not lib().delete_local(name) and not skill_sources.remove(lib(), name):
            raise HTTPException(404, f"{name!r} isn't one of your own or installed skills")
        return skills_view(lib())

    return router
