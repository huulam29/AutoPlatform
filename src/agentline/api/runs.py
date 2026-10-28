"""Run view API (read-only; approvals stay CLI-only, decision 6).

Routes (under `/api`):
- `GET /runs?status=&limit=`  newest first
- `GET /runs/{id}`            run + executions + lineage + effects + costs + `pipeline` (the
                              graph model of the file whose `name:` is the run's flow) +
                              `next_commands` (CLI strings for waiting gates / needs-review)
- `GET /artifacts/{hash}`     metadata + value; text over 1 MiB is cut (`truncated: true`)
- `GET /runs/start-info?pipeline=`  the start point: `{node, input, template, fields}`
- `POST /runs`                `{pipeline, payload | text}` starts a run
- `GET /runs/{id}/messages?after=`  the conversation with the chat start node + `thinking`
- `POST /runs/{id}/messages`  `{text}`; `POST /runs/{id}/messages/{mid}/apply` `{index,
                              dismiss?}` confirms (or dismisses) a proposed action
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agentline import __version__, restart
from agentline.api.editor import PipelineFiles
from agentline.app import App, default_actor
from agentline.artifacts import artifact_dict
from agentline.errors import ValidationError
from agentline.ledger.base import Execution, Run
from agentline.states import TERMINAL_RUN

ARTIFACT_CAP = 1024 * 1024
_HASH = re.compile(r"^[0-9a-f]{16,128}$")


def run_dict(run: Run) -> dict[str, Any]:
    d = asdict(run)
    d["terminal"] = run.status in TERMINAL_RUN
    return d


def execution_dict(x: Execution) -> dict[str, Any]:
    return {
        "id": x.id,
        "node_id": x.node_id,
        "iteration": x.iteration,
        "key": x.key,
        "status": x.status,
        "attempt": x.attempt,
        "reclaims": x.reclaims,
        "cost_usd": x.cost_usd,
        "reason": x.reason,
        "error": x.error,
        "detail": x.detail,
        "correlation_key": x.correlation_key,
        "result_ref": x.result_ref,
        "created_at": x.created_at,
        "started_at": x.started_at,
        "finished_at": x.finished_at,
    }


def task_dict(t: Any) -> dict[str, Any]:
    d = asdict(t)
    d.pop("result", None)  # the node's output has them; keep the payload small
    return d


def node_counts(execs: list[Execution], tasks: list[Any]) -> dict[str, dict[str, int]]:
    """Per node: how many tasks are pending / running / done / failed, for the run view.
    A node without for_each counts its latest execution as one task."""
    out: dict[str, dict[str, int]] = {}
    latest: dict[str, Execution] = {}
    for x in execs:
        if x.node_id not in latest or x.iteration >= latest[x.node_id].iteration:
            latest[x.node_id] = x
    by_exec: dict[str, list[Any]] = {}
    for t in tasks:
        by_exec.setdefault(t.execution_id, []).append(t)
    for node_id, x in latest.items():
        c = {"total": 0, "pending": 0, "running": 0, "done": 0, "failed": 0}
        own = by_exec.get(x.id)
        if own:
            for t in own:
                c["total"] += 1
                c[t.status if t.status in c else "pending"] += 1
        else:
            c["total"] = 1
            key = {"done": "done", "running": "running", "dead": "failed", "failed": "failed",
                   "needs-review": "failed"}.get(x.status, "pending")  # fmt: skip
            c[key] = 1
        out[node_id] = c
    return out


def next_commands(run: Run, execs: list[Execution]) -> list[dict[str, Any]]:
    """What a human can do next, as `agentline` CLI commands (there are no buttons)."""
    out: list[dict[str, Any]] = []
    gates = [x for x in execs if x.status == "waiting" and x.detail.get("kind") == "gate"]
    for g in gates:
        node = f" --node {g.node_id}" if len(gates) > 1 else ""
        approvers = g.detail.get("approvers") or []
        who = f" (approvers: {', '.join(approvers)})" if approvers else ""
        out.append(
            {
                "node": g.node_id,
                "command": f"agentline approve {run.id}{node}",
                "description": f"approve gate {g.key}{who}",
            }
        )
        out.append(
            {
                "node": g.node_id,
                "command": f"agentline reject {run.id}{node}",
                "description": f"reject gate {g.key}",
            }
        )
    reviews = [x for x in execs if x.status == "needs-review"]
    for r in reviews:
        node = f" --node {r.node_id}" if len(reviews) > 1 else ""
        why = f" ({r.reason})" if r.reason else ""
        if r.reason == "missing_tools" and r.detail.get("image_proposal"):
            out.append(
                {
                    "node": r.node_id,
                    "command": f"agentline approve {run.id}{node} --proposal <id from runs show>",
                    "description": f"apply the image proposal for {r.key}, build, retry",
                }
            )
        for action in ("retry", "skip", "fail"):
            out.append(
                {
                    "node": r.node_id,
                    "command": f"agentline resolve {run.id}{node} --action {action}",
                    "description": f"{action} {r.key}{why}",
                }
            )
    out.append({"node": None, "command": f"agentline runs show {run.id}", "description": "details"})
    return out


def artifact_payload(app: App, digest: str, cap: int = ARTIFACT_CAP) -> dict[str, Any] | None:
    store = app.scheduler.artifacts
    with app.ledger.tx() as tx:
        meta = store.meta(tx, digest)
        if meta is None:
            data = tx.get_blob(digest)  # a pre-Phase-5 result blob
            if data is None:
                return None
            info: dict[str, Any] = {
                "hash": digest,
                "kind": "json",
                "media_type": "application/json",
                "size": len(data),
            }
        else:
            info = artifact_dict(meta)
            if meta.storage == "file" and meta.size > cap:
                path = Path(meta.path) if meta.path else store.file_path(digest)
                with path.open("rb") as f:
                    data = f.read(cap)
            else:
                data = store.read_bytes(tx, digest) or b""
    truncated = len(data) > cap or info.get("size", 0) > cap
    data = data[:cap]
    if info.get("kind") == "json" and not truncated:
        return {**info, "encoding": "json", "value": json.loads(data), "truncated": False}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if truncated:  # a multi-byte character may have been cut
            try:
                text = data[:-3].decode("utf-8")
            except UnicodeDecodeError:
                text = None
        else:
            text = None
    if text is not None:
        return {**info, "encoding": "text", "value": text, "truncated": truncated}
    return {
        **info,
        "encoding": "base64",
        "value": base64.b64encode(data).decode(),
        "truncated": truncated,
    }


class RetryBody(BaseModel):
    node: str | None = None


class GateBody(BaseModel):
    node: str | None = None
    comment: str = ""


class StartBody(BaseModel):
    pipeline: str
    payload: dict[str, Any] = Field(default_factory=dict)
    text: str | None = None  # text / chat pipelines: becomes {"text": ...}
    dedupe_key: str | None = None


class MessageBody(BaseModel):
    text: str


class CardBody(BaseModel):
    index: int
    dismiss: bool = False


def create_runs_router(
    app: App, files: PipelineFiles | None = None, runchat: Any = None
) -> APIRouter:
    router = APIRouter()

    @router.get("/runs/start-info")
    def start_info(pipeline: str) -> dict[str, Any]:
        from agentline.pipeline.start import start_info as info

        if pipeline not in app.flow_names():
            raise HTTPException(404, f"no pipeline {pipeline!r}")
        spec = app.graph(pipeline).meta.get("spec")
        if spec is None:  # a flow written in Python: JSON in, nothing known about it
            return {"node": None, "input": "json", "template": {}, "fields": []}
        return info(spec)

    @router.get("/runs/{run_id}/messages")
    def messages(run_id: str, after: int = 0) -> dict[str, Any]:
        from agentline import runchat as rc

        if app.get_run(run_id) is None:
            raise HTTPException(404, f"no run {run_id}")
        msgs = rc.thread(app, run_id, after=max(0, after))
        return {
            "messages": [rc.message_dict(m) for m in msgs],
            "thinking": rc.thinking(app, run_id, runchat),
        }

    @router.post("/runs/{run_id}/messages")
    def send_message(run_id: str, body: MessageBody) -> dict[str, Any]:
        from agentline import runchat as rc

        try:
            return asyncio.run(rc.say(app, run_id, body.text, via="app"))
        except ValidationError as e:
            raise HTTPException(404 if str(e).startswith("no run") else 409, str(e)) from None

    @router.post("/runs/{run_id}/messages/{message_id}/apply")
    def apply_card(run_id: str, message_id: int, body: CardBody) -> dict[str, Any]:
        from agentline import runchat as rc

        try:
            who = default_actor()
            card = asyncio.run(
                rc.apply_card(app, run_id, message_id, body.index, actor=who, dismiss=body.dismiss)
            )
        except ValidationError as e:
            raise HTTPException(409, str(e)) from None
        return {"card": card}

    @router.get("/runs")
    def list_runs(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        with app.ledger.tx() as tx:
            runs = tx.list_runs(status or None, limit)
        return {"runs": [run_dict(r) for r in runs]}

    @router.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        with app.ledger.tx() as tx:
            run = tx.get_run(run_id)
            if run is None:
                raise HTTPException(404, f"no run {run_id}")
            execs = tx.list_executions(run_id)
            effects = tx.list_effects(run_id)
            costs = tx.list_costs(run_id)
            tasks = tx.list_tasks(run_id)
        pipeline = None
        if files is not None and (found := files.find_by_pipeline_name(run.flow)):
            pipeline = {"name": found[0], "model": found[1]}
        return {
            "run": run_dict(run),
            "executions": [execution_dict(x) for x in execs],
            "lineage": app.lineage(run_id),
            "effects": [asdict(e) for e in effects],
            "costs": costs,
            "pipeline": pipeline,
            "next_commands": next_commands(run, execs),
            "tasks": [task_dict(t) for t in tasks],
            "node_counts": node_counts(execs, tasks),
        }

    @router.get("/runs/{run_id}/journal")
    def journal(run_id: str, node: str | None = None, limit: int = 2000) -> dict[str, Any]:
        with app.ledger.tx() as tx:
            if tx.get_run(run_id) is None:
                raise HTTPException(404, f"no run {run_id}")
            entries = tx.list_journal(run_id, node_id=node or None, limit=max(1, min(limit, 5000)))
        return {"journal": [asdict(e) for e in entries]}

    @router.get("/runs/{run_id}/fix")
    def fix_request(run_id: str) -> dict[str, Any]:
        """A fix request for the editor assistant about this run (see agentline.fixit)."""
        from agentline import fixit

        found = fixit.for_run(app, run_id)
        if found is None:
            raise HTTPException(404, f"no run {run_id}")
        pipeline = None
        if files is not None and (hit := files.find_by_pipeline_name(found["flow"])):
            pipeline = hit[0]
        return {"prompt": fixit.request_text(found["text"], run_id), "pipeline": pipeline,
                "run": run_id, "flow": found["flow"]}  # fmt: skip

    @router.post("/runs/{run_id}/retry")
    def retry(run_id: str, body: RetryBody) -> dict[str, Any]:
        try:
            n = asyncio.run(
                app.retry(run_id, node_id=body.node or None, actor=default_actor(), via="app")
            )
        except ValidationError as e:
            raise HTTPException(404 if str(e).startswith("no run") else 409, str(e)) from None
        return {"run_id": run_id, "node": body.node, "retried": n}

    @router.get("/approvals")
    def approvals() -> list[dict[str, Any]]:
        """Every gate waiting for a human, across runs (what the Mac app notifies about)."""
        out = []
        with app.ledger.tx() as tx:
            for run in tx.list_runs("waiting", 1000):
                for x in tx.list_executions(run.id):
                    if x.status == "waiting" and x.detail.get("kind") == "gate":
                        out.append(
                            {
                                "run_id": run.id,
                                "pipeline": run.flow,
                                "node_id": x.node_id,
                                "execution_id": x.id,
                                "prompt": x.detail.get("prompt", ""),
                                "approvers": x.detail.get("approvers") or [],
                                "waiting_since": x.started_at or x.created_at,
                                "payload": run.payload,
                            }
                        )
        return sorted(out, key=lambda a: a["waiting_since"] or 0)

    def _decide(run_id: str, body: GateBody, verb: str) -> dict[str, Any]:
        actor = default_actor()
        fn = app.approve if verb == "approve" else app.reject
        try:
            gate = asyncio.run(fn(run_id, body.node, actor=actor, comment=body.comment, via="app"))
        except PermissionError as e:
            raise HTTPException(403, str(e)) from None
        except ValidationError as e:
            status = 404 if str(e).startswith("no run") else 409
            raise HTTPException(status, str(e)) from None
        return {"run_id": run_id, "node_id": gate.node_id, "actor": actor}

    @router.post("/runs/{run_id}/approve")
    def approve(run_id: str, body: GateBody) -> dict[str, Any]:
        return _decide(run_id, body, "approve")

    @router.post("/runs/{run_id}/reject")
    def reject(run_id: str, body: GateBody) -> dict[str, Any]:
        return _decide(run_id, body, "reject")

    @router.post("/runs")
    def start_run(body: StartBody) -> dict[str, Any]:
        if body.pipeline not in app.flow_names():
            raise HTTPException(
                404, f"no pipeline {body.pipeline!r} (have: {', '.join(app.flow_names())})"
            )
        payload: Any = {"text": body.text} if body.text is not None else body.payload
        try:
            sub = app.submit_sync(body.pipeline, payload, body.dedupe_key)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return {"run_id": sub.run_id, "created": sub.created}

    @router.get("/logs")
    def logs(limit: int = 500) -> dict[str, Any]:
        """The engine's last log lines (redacted), newest last."""
        from agentline.logs import RING

        lines = list(RING.lines)[-max(1, min(limit, 5000)) :]
        return {"lines": lines}

    @router.get("/status")
    def status() -> dict[str, Any]:
        poll = app.runtime.get("jira_poll") or {"enabled": False}
        return {
            "version": __version__,
            "pipelines": app.flow_names(),
            "jira_poll": poll,
            "keys_managed": os.environ.get("AGENTLINE_KEYS_MANAGED") or None,
            "restart_needed": restart.pending(),
        }

    @router.get("/artifacts/{digest}")
    def get_artifact(digest: str) -> dict[str, Any]:
        if not _HASH.match(digest):
            raise HTTPException(400, "bad artifact hash")
        art = artifact_payload(app, digest)
        if art is None:
            raise HTTPException(404, f"no artifact {digest}")
        return art

    return router
