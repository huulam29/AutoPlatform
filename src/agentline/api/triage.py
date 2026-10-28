"""Failure triage API (agentline.triage).

GET  /triage?run=          {records (newest first), github: {repo, token_set, ready, auto,
                           error}}
GET  /triage/{id}
POST /triage {run, node?}  triage that run (its failed node, or `node`) now and return the
                           record; a click always redoes it. Skipped when the run's budget is
                           used up (triage counts against it)
POST /triage/{id}/github   send an infrastructure triage's report to GitHub (once)

App reports (agentline's own problems, kept off Problems):
GET    /app-reports        {reports, github}: every crash / error report (triaged as
                           infrastructure first, no model call) and infrastructure verdicts
DELETE /app-reports/{id}   remove one (a crash report goes with its record)
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentline import triage as tr
from agentline.errors import ValidationError


class TriageBody(BaseModel):
    run: str
    node: str | None = None


def create_triage_router(
    app: Any,
    files: Any,
    issues: Any,
    store: tr.TriageStore | None = None,
    incidents: Any = None,
) -> APIRouter:
    router = APIRouter()
    store = store or tr.TriageStore()

    @router.get("/triage")
    def list_triage(run: str | None = None) -> dict[str, Any]:
        return {"records": store.list(run), "github": tr.github_status(files.config)}

    @router.get("/triage/{tid}")
    def get_triage(tid: str) -> dict[str, Any]:
        rec = store.get(tid)
        if rec is None:
            raise HTTPException(404, f"no triage {tid}")
        return rec

    @router.post("/triage")
    async def run_triage(body: TriageBody) -> dict[str, Any]:
        f = await asyncio.to_thread(tr.failure_for, app, body.run, body.node)
        if f is None:
            what = f"node {body.node!r} in run {body.run}" if body.node else f"run {body.run}"
            raise HTTPException(404, f"no {what}")
        return await tr.triage(app, files, f, store=store, issues=issues, manual=True, force=True)

    @router.post("/triage/{tid}/github")
    def report(tid: str) -> dict[str, Any]:
        from agentline.integrations.http import ApiError

        rec = store.get(tid)
        if rec is None:
            raise HTTPException(404, f"no triage {tid}")
        try:
            return tr.report_github(rec, files.config, store, issues)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        except ApiError as e:
            raise HTTPException(502, f"GitHub said no: {e}") from None

    @router.get("/app-reports")
    def app_reports() -> dict[str, Any]:
        reports = tr.app_reports(store, incidents, files.config, app.redactor)
        return {"reports": reports, "github": tr.github_status(files.config)}

    @router.delete("/app-reports/{tid}")
    def delete_report(tid: str) -> dict[str, Any]:
        rec = store.get(tid)
        if rec is None or rec.get("verdict") != "infrastructure":
            raise HTTPException(404, f"no app report {tid}")
        if rec.get("incident") and incidents is not None:
            incidents.delete(rec["incident"])
        store.delete(tid)
        return {"deleted": tid}

    return router
