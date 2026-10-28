"""Issues API: one tracked issue per problem (crash/error report or failed run), local only.

GET    /issues?status=           sync with the failed runs, then the issues (newest activity
                                 first) and counts; status: open | in_progress | fixed |
                                 wont_fix | active | closed. App problems (crash reports,
                                 infrastructure triages) aren't issues here: see /app-reports
GET    /issues/{number}
PATCH  /issues/{number}          {"status"?, "note"?, "title"?}  a status change records the
                                 note with it in the history; a note alone is added to notes
GET    /issues/{number}/fix   {prompt, pipeline, run}: a fix request for the editor assistant
DELETE /issues/{number}
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentline.errors import ValidationError
from agentline.incidents import IncidentStore
from agentline.issues import FAILED_RUN_STATUSES, STATUSES, IssueStore


class IssueBody(BaseModel):
    status: str | None = None
    note: str | None = None
    title: str | None = None


def create_issues_router(
    app: Any, incidents: IncidentStore, issues: IssueStore, files: Any = None
) -> APIRouter:
    router = APIRouter()

    def failed_runs() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with app.ledger.tx() as tx:
            for s in FAILED_RUN_STATUSES:
                out += [asdict(r) for r in tx.list_runs(s, 500)]
        return out

    @router.get("/issues")
    def list_issues(status: str | None = None) -> dict[str, Any]:
        from agentline.triage import TriageStore

        synced = issues.sync(incidents.list(limit=1000), failed_runs())
        # runs triaged as infrastructure are app problems too, even if the issue missed the link
        infra = {
            r["run"]
            for r in TriageStore().list(limit=1000)
            if r.get("verdict") == "infrastructure" and r.get("run")
        }

        def mine(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                i
                for i in items
                if not (i["source"]["kind"] == "run" and i["source"]["ref"] in infra)
            ]

        every = mine(issues.list())
        counts = {s: sum(1 for i in every if i.get("status") == s) for s in STATUSES}
        return {"issues": mine(issues.list(status or None)), "counts": counts, "synced": synced}

    @router.get("/issues/{number}")
    def get_issue(number: int) -> dict[str, Any]:
        rec = issues.get(number)
        if rec is None:
            raise HTTPException(404, f"no issue #{number}")
        return rec

    @router.patch("/issues/{number}")
    def update_issue(number: int, body: IssueBody) -> dict[str, Any]:
        try:
            return issues.update(number, status=body.status, note=body.note, title=body.title)
        except KeyError:
            raise HTTPException(404, f"no issue #{number}") from None
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None

    @router.get("/issues/{number}/fix")
    def fix_request(number: int) -> dict[str, Any]:
        """What to send the editor assistant to fix this issue, and which pipeline to open."""
        from agentline import fixit

        rec = issues.get(number)
        if rec is None:
            raise HTTPException(404, f"no issue #{number}")
        src = rec.get("source") or {}
        incident = incidents.get(str(src.get("ref"))) if src.get("kind") == "incident" else None
        out = fixit.for_issue(app, rec, incident)
        out["pipeline"] = None
        if (
            out.get("flow")
            and files is not None
            and (hit := files.find_by_pipeline_name(out["flow"]))
        ):
            out["pipeline"] = hit[0]
        return out

    @router.delete("/issues/{number}")
    def delete_issue(number: int) -> dict[str, Any]:
        if not issues.delete(number):
            raise HTTPException(404, f"no issue #{number}")
        return {"deleted": number}

    return router
