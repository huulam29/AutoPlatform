"""Incidents API: the crash and failure reports in `$AGENTLINE_HOME/incidents/` (local only).

GET    /incidents?limit=      summaries, newest first, plus the directory
GET    /incidents/{id}        the full record (traceback, context, attachments)
POST   /incidents/{id}/seen
DELETE /incidents/{id}
DELETE /incidents             clear all
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from agentline.incidents import IncidentStore


def create_incidents_router(store: IncidentStore) -> APIRouter:
    router = APIRouter()

    @router.get("/incidents")
    def list_incidents(limit: int = 200) -> dict[str, Any]:
        items = store.list(max(1, min(limit, 1000)))
        return {
            "incidents": items,
            "dir": str(store.root),
            "new": sum(1 for i in items if i.get("status") == "new"),
        }

    @router.get("/incidents/{iid}")
    def get_incident(iid: str) -> dict[str, Any]:
        rec = store.get(iid)
        if rec is None:
            raise HTTPException(404, f"no incident {iid}")
        return rec

    @router.post("/incidents/{iid}/seen")
    def seen(iid: str) -> dict[str, Any]:
        if not store.mark_seen(iid):
            raise HTTPException(404, f"no incident {iid}")
        return {"id": iid, "status": "seen"}

    @router.delete("/incidents/{iid}")
    def delete(iid: str) -> dict[str, Any]:
        if not store.delete(iid):
            raise HTTPException(404, f"no incident {iid}")
        return {"deleted": iid}

    @router.delete("/incidents")
    def clear() -> dict[str, Any]:
        return {"deleted": store.clear()}

    return router
