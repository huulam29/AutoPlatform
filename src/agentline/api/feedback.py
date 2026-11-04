"""Run feedback, metrics, reports, insights and telemetry settings (phase 58).

GET  /runs/{run}/feedback            {run, entries, summary}
POST /runs/{run}/feedback            {rating: up|down|none, comment?, node?, iteration?}
GET  /runs/{run}/metrics             per-run and per-node metrics (agentline.metrics)
GET  /runs/{run}/report?format=md|json   the redacted run report, as a download
GET  /insights?days=30&pipeline=     trends of finished runs
GET  /telemetry                      [telemetry] settings + last export (no header values)
PUT  /telemetry                      {enabled?, endpoint?, service_name?, headers_secret?}
POST /telemetry/test                 send one test span
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from agentline import feedback as fb
from agentline import metrics, runreport, telemetry
from agentline.errors import ValidationError


class FeedbackBody(BaseModel):
    rating: str
    comment: str = ""
    node: str | None = None
    iteration: int | None = None
    by: str | None = None


class TelemetryBody(BaseModel):
    enabled: bool | None = None
    endpoint: str | None = None
    service_name: str | None = None
    headers_secret: str | None = None


def create_feedback_router(app: Any, files: Any) -> APIRouter:
    router = APIRouter()

    def has_run(run_id: str) -> None:
        with app.ledger.tx() as tx:
            if tx.get_run(run_id) is None:
                raise HTTPException(404, f"no run {run_id}")

    @router.get("/runs/{run_id}/feedback")
    def get_feedback(run_id: str) -> dict[str, Any]:
        has_run(run_id)
        rows = fb.entries(app, run_id)
        return {"run": run_id, "entries": rows, "summary": fb.summarize(rows)}

    @router.post("/runs/{run_id}/feedback")
    def post_feedback(run_id: str, body: FeedbackBody) -> dict[str, Any]:
        try:
            entry = fb.add(
                app,
                run_id,
                body.rating,
                body.comment,
                node=body.node,
                iteration=body.iteration,
                by=body.by or "app",
            )
        except KeyError:
            raise HTTPException(404, f"no run {run_id}") from None
        except ValidationError as e:
            raise HTTPException(400, str(e)) from None
        return {"entry": entry, "summary": fb.summary(app, run_id)}

    @router.get("/runs/{run_id}/metrics")
    def get_metrics(run_id: str) -> dict[str, Any]:
        m = metrics.run_metrics(app, run_id)
        if m is None:
            raise HTTPException(404, f"no run {run_id}")
        return m

    @router.get("/runs/{run_id}/report")
    def get_report(run_id: str, format: str = Query("md", pattern="^(md|json)$")) -> Response:
        rep = runreport.build(app, run_id)
        if rep is None:
            raise HTTPException(404, f"no run {run_id}")
        name = f"run-{run_id}.{format}"
        disp = {"Content-Disposition": f'attachment; filename="{name}"'}
        if format == "json":
            body = json.dumps(rep, indent=2, default=str)
            return Response(body, media_type="application/json", headers=disp)
        return Response(runreport.markdown(rep), media_type="text/markdown", headers=disp)

    @router.get("/insights")
    def get_insights(days: int = 30, pipeline: str | None = None) -> dict[str, Any]:
        return metrics.insights(app, days, pipeline or None)

    @router.get("/telemetry")
    def get_telemetry() -> dict[str, Any]:
        return telemetry.status(files.config)

    @router.put("/telemetry")
    async def put_telemetry(body: TelemetryBody) -> dict[str, Any]:
        from agentline.api.settings import config_path
        from agentline.config import Config

        path = config_path(files.config)
        try:
            await asyncio.to_thread(telemetry.save, path, body.model_dump(exclude_none=True))
        except ValidationError as e:
            raise HTTPException(400, str(e)) from None
        files.config = Config.load(path)
        files.changed("telemetry settings")
        return telemetry.status(files.config)

    @router.post("/telemetry/test")
    async def test_telemetry() -> dict[str, Any]:
        conf = telemetry.config(files.config)
        return await asyncio.to_thread(telemetry.test_send, app, conf)

    return router
