"""Run review and harness learning API (agentline.review, agentline.probe, agentline.lessons).

GET  /reviews?run=&flow=          {records (newest first), config ([review] with defaults)}
GET  /reviews/{id}
POST /reviews {run}               review that run now (a click always redoes it); skipped when
                                  the run's budget is used up
POST /reviews/{id}/probe {index}  replay proposal `index` (a pipeline edit) on the node it
                                  changes and compare; returns the probe
POST /reviews/{id}/apply {index}  apply proposal `index` (a pipeline edit; history-recorded) and
                                  remember it for the follow-up (helped / hurt / no effect after
                                  the next runs); {adoption}
GET  /adoptions?flow=             adopted edits and their verdicts
GET  /lessons                     {pipelines: [{pipeline, flow, file, lessons, adoptions}]}: every
                                  pipeline with lessons or adopted edits (the Lessons page)
GET  /lessons/{pipeline}          {pipeline, flow, file, lessons, adoptions}
POST /lessons/{pipeline}          {op: "add", node, component, cue, lesson, evidence,
                                  confidence, review?} | {op: "status", id, status, note?}
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentline import lessons as lib
from agentline import review as rv
from agentline.errors import ValidationError


class ReviewBody(BaseModel):
    run: str


class IndexBody(BaseModel):
    index: int


class LessonBody(BaseModel):
    op: str = "add"
    id: int | None = None
    status: str | None = None
    note: str = ""
    node: str = ""
    component: str = "prompt"
    cue: str = ""
    lesson: str = ""
    evidence: str = ""
    confidence: str = "low"
    review: str = ""
    runs: str = ""


def _flow_of(files: Any, path: Path) -> str:
    with contextlib.suppress(Exception):
        return str(
            files.model(path.read_text(encoding="utf-8"), path, validate=False).get("name") or ""
        )
    return ""


def create_reviews_router(app: Any, files: Any, store: rv.ReviewStore | None = None) -> APIRouter:
    router = APIRouter()
    store = store or rv.ReviewStore()

    def get(rid: str) -> dict[str, Any]:
        rec = store.get(rid)
        if rec is None:
            raise HTTPException(404, f"no review {rid}")
        return rec

    @router.get("/reviews")
    def list_reviews(run: str | None = None, flow: str | None = None) -> dict[str, Any]:
        return {"records": store.list(run, flow), "config": rv.config(files.config)}

    @router.get("/reviews/{rid}")
    def get_review(rid: str) -> dict[str, Any]:
        return get(rid)

    @router.post("/reviews")
    async def run_review(body: ReviewBody) -> dict[str, Any]:
        try:
            return await rv.review(app, files, body.run, store=store, manual=True, force=True)
        except KeyError:
            raise HTTPException(404, f"no run {body.run}") from None

    @router.post("/reviews/{rid}/probe")
    async def run_probe(rid: str, body: IndexBody) -> dict[str, Any]:
        from agentline import probe

        get(rid)
        try:
            return await probe.probe(app, files, rid, body.index, store=store, force=True)
        except KeyError:
            raise HTTPException(404, f"no proposal {body.index} in review {rid}") from None

    @router.post("/reviews/{rid}/apply")
    def apply(rid: str, body: IndexBody) -> dict[str, Any]:
        rec = get(rid)
        try:
            return rv.apply_edit(app, files, rec, body.index, store)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None

    @router.get("/adoptions")
    def adoptions(flow: str | None = None) -> dict[str, Any]:
        return {"adoptions": store.adoptions.list(flow)}

    def library(path: Path, adoptions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        flow = _flow_of(files, path)
        if adoptions is None:
            adoptions = store.adoptions.list(flow) if flow else []
        else:
            adoptions = [a for a in adoptions if flow and a.get("flow") == flow]
        return {
            "pipeline": files.name_of(path),
            "flow": flow,
            "file": lib.path_for(path).relative_to(files.root).as_posix(),
            "lessons": [x.as_dict() for x in lib.read(path)],
            "adoptions": adoptions,
        }

    @router.get("/lessons")
    def all_lessons() -> dict[str, Any]:
        adoptions = store.adoptions.list()
        out = [library(p, adoptions) for p in files.files()]
        return {"pipelines": [x for x in out if x["lessons"] or x["adoptions"]]}

    @router.get("/lessons/{name:path}")
    def get_lessons(name: str) -> dict[str, Any]:
        path = files.resolve(name)
        if not path.is_file():
            raise HTTPException(404, f"no pipeline {name}")
        return library(path)

    @router.post("/lessons/{name:path}")
    def post_lesson(name: str, body: LessonBody) -> dict[str, Any]:
        from agentline import history

        path = files.resolve(name)
        if not path.is_file():
            raise HTTPException(404, f"no pipeline {name}")
        title = _flow_of(files, path) or files.name_of(path)
        try:
            with files.lock:
                if body.op == "add":
                    a = body.model_dump()
                    a["pipeline"] = files.name_of(path)
                    x = lib.add(path, title, a)
                    rec = store.get(body.review) if body.review else None
                    if rec is not None:  # the review's lesson card shows as applied
                        rec["lesson_added"] = x.id
                        store.save(rec)
                elif body.op == "status" and body.id is not None:
                    x = lib.set_status(path, title, body.id, str(body.status or ""), body.note)
                else:
                    raise ValidationError("op must be add (with the lesson) or status (with id)")
                history.record(files.root, f"lesson L{x.id}: {files.name_of(path)}")
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        except KeyError:
            raise HTTPException(404, f"no lesson L{body.id} in {name}") from None
        return x.as_dict()

    return router
