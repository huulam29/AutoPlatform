"""Incidents API: the crash and failure reports in `$AGENTLINE_HOME/incidents/` (local only).

GET    /incidents?limit=      summaries, newest first, plus the directory
GET    /incidents/export      every report as one .zip (the Mac app's Export report layout:
                              incidents/<id>.json + attachments, system.txt, engine.log,
                              summary.md), redacted
GET    /incidents/{id}        the full record (traceback, context, attachments)
POST   /incidents/{id}/seen
DELETE /incidents/{id}
DELETE /incidents             clear all
"""

from __future__ import annotations

import io
import json
import platform
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from agentline.incidents import IncidentStore, platform_text

LOG_LINES = 500


def _safe_relative(rel: str) -> bool:
    p = Path(rel)
    return bool(rel) and not p.is_absolute() and ".." not in p.parts


def _system_text(version: str) -> str:
    lines = [
        f"agentline engine {version}",
        f"platform: {platform_text()}",
        f"python: {sys.version.split()[0]} ({platform.python_implementation()})",
        f"machine: {platform.machine()}",
    ]
    return "\n".join(lines) + "\n"


def _summary(items: list[dict[str, Any]], version: str, generated: float) -> str:
    when = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(generated))
    md = f"# agentline report\n\nGenerated {when}. Engine {version}, {platform_text()}.\n\n"
    if not items:
        return md + "No reports.\n"
    for i in items:
        seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(i.get("last_seen") or 0))
        times = f" ({i.get('count')} times)" if (i.get("count") or 1) > 1 else ""
        md += f"- **{i.get('title', '')}** [{i.get('kind', '')}] last {seen}{times}\n"
    return md


def export_zip(store: IncidentStore, log_lines: list[str], generated: float) -> bytes:
    """The report zip. Incident files are already redacted when written; the log is redacted
    here (the store's redactor)."""
    items = store.list(limit=100_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in items:
            iid = str(i.get("id") or "")
            rec = store.get(iid)
            if rec is None:
                continue
            z.writestr(f"incidents/{iid}.json", json.dumps(rec, indent=2, default=str))
            for rel in rec.get("attachments") or []:
                src = store.root / str(rel)
                if (
                    _safe_relative(str(rel))
                    and src.is_file()
                    and src.resolve().is_relative_to(store.root.resolve())
                ):
                    z.write(src, f"incidents/{rel}")
        z.writestr("system.txt", _system_text(store.version))
        lines = [store._redact(x) for x in log_lines[-LOG_LINES:]]
        z.writestr("engine.log", "\n".join(lines) + ("\n" if lines else ""))
        z.writestr("summary.md", _summary(items, store.version, generated))
    return buf.getvalue()


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

    @router.get("/incidents/export")
    def export() -> Response:
        from agentline.logs import RING

        now = time.time()
        data = export_zip(store, list(RING.lines), now)
        name = time.strftime("agentline-report-%Y-%m-%d-%H%M.zip", time.localtime(now))
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

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
