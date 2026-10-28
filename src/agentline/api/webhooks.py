"""Webhook server: GitHub reviews resume `github.review` waits; Jira assignments start runs.

- `POST /webhooks/github`: HMAC-SHA256 `X-Hub-Signature-256` over the raw body with
  GITHUB_WEBHOOK_SECRET (constant-time). Replays rejected by `X-GitHub-Delivery` (409); a
  review the poller already delivered (same review id) is answered 200 `duplicate`.
  `pull_request_review` / `submitted` with state approved or changes_requested →
  `deliver_event("github:pr:<owner>/<repo>#<n>", {state, comments, reviewer, commit_id, ...})`.
  `commented` (and `dismissed`) reviews are ignored: they are not a decision, and the
  pipeline's router has no edge for them.
- `POST /webhooks/jira`: shared secret in `X-Agentline-Secret` or `?secret=` (JIRA_WEBHOOK_SECRET,
  constant-time). An assignment submits every pipeline whose trigger is
  `{type: webhook, source: jira, event: issue_assigned}` and whose `filter` matches.
- `GET /healthz`.

401 bad signature/secret, 202 accepted (with run ids), 200 `ignored` for events that don't
match, 409 for a replayed GitHub delivery.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentline.actions.github import review_key, review_payload
from agentline.app import App
from agentline.deliveries import claim_delivery, deliver_once, ensure_table
from agentline.errors import ValidationError
from agentline.integrations.github import GitHubClient

log = logging.getLogger("agentline.api")

JIRA_FILTER_KEYS = frozenset({"project", "assignee", "issue_type", "labels"})


def verify_github_signature(secret: str | None, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.encode())


def verify_shared_secret(secret: str | None, given: str | None) -> bool:
    if not secret or not given:
        return False
    return hmac.compare_digest(secret.encode(), given.encode())


def _ignored(reason: str) -> JSONResponse:
    return JSONResponse({"status": "ignored", "reason": reason}, status_code=200)


def _unauthorized(reason: str) -> JSONResponse:
    return JSONResponse({"status": "unauthorized", "reason": reason}, status_code=401)


# -- jira ---------------------------------------------------------------------------------
def _identities(user: Any) -> set[str]:
    if isinstance(user, str):
        return {user}
    if not isinstance(user, dict):
        return set()
    keys = ("accountId", "name", "key", "emailAddress", "displayName")
    return {str(user[k]) for k in keys if user.get(k)}


def jira_assignment(body: dict[str, Any]) -> dict[str, Any] | None:
    """Pull `{issue_key, assignee, identities, project, ...}` out of an assignment event."""
    event = str(body.get("webhookEvent") or "")
    etype = str(body.get("issue_event_type_name") or "")
    items = (body.get("changelog") or {}).get("items") or []
    change = next(
        (i for i in items if i.get("field") == "assignee" or i.get("fieldId") == "assignee"), None
    )
    assigned = (
        event in ("issue_assigned", "jira:issue_assigned")
        or etype == "issue_assigned"
        or (event == "jira:issue_updated" and change is not None)
    )
    issue = body.get("issue") or {}
    if not assigned or not issue.get("key"):
        return None
    fields = issue.get("fields") or {}
    user = fields.get("assignee") or (change or {}).get("to")
    ids = _identities(user)
    if not ids:
        return None  # unassigned
    assignee = (
        user.get("accountId") or user.get("name") or user.get("emailAddress")
        if isinstance(user, dict)
        else str(user)
    )
    key = str(issue["key"])
    project = (fields.get("project") or {}).get("key") or key.split("-")[0]
    marker = (body.get("changelog") or {}).get("id") or body.get("timestamp") or ""
    return {
        "issue_key": key,
        "assignee": str(assignee),
        "identities": ids,
        "project": str(project),
        "issue_type": (fields.get("issuetype") or {}).get("name"),
        "labels": list(fields.get("labels") or []),
        "marker": str(marker),
    }


def _as_list(v: Any) -> list[str]:
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]


def filter_matches(flt: dict[str, Any], a: dict[str, Any]) -> bool:
    for k, want in flt.items():
        wanted = _as_list(want)
        if k == "project" and a["project"] not in wanted:
            return False
        if k == "assignee" and not a["identities"] & set(wanted):
            return False
        if k == "issue_type" and a["issue_type"] not in wanted:
            return False
        if k == "labels" and not set(a["labels"]) & set(wanted):
            return False
    return True


def _is_jira_assign(p: Any) -> bool:
    t = p.spec.trigger
    return bool(t.type == "webhook" and t.source == "jira" and t.event == "issue_assigned")


# -- app ------------------------------------------------------------------------------------
def create_app(
    app: App,
    pipelines: Sequence[Any],
    *,
    github_secret: str | None = None,
    jira_secret: str | None = None,
    github_client: Callable[[], GitHubClient | None] | None = None,
) -> FastAPI:
    """Secrets default to GITHUB_WEBHOOK_SECRET / JIRA_WEBHOOK_SECRET; an endpoint whose secret
    is unset rejects everything. `github_client` (default: from env when GITHUB_TOKEN is set)
    fetches a review's line comments; without it only the review body is used."""
    github_secret = github_secret or os.environ.get("GITHUB_WEBHOOK_SECRET")
    jira_secret = jira_secret or os.environ.get("JIRA_WEBHOOK_SECRET")
    if github_client is None:

        def github_client() -> GitHubClient | None:
            return GitHubClient.from_env() if os.environ.get("GITHUB_TOKEN") else None

    from agentline.live import current

    for p in current(pipelines):
        try:
            app.graph(p.name)
        except ValidationError:
            app.register_pipeline(p)

    def jira_targets() -> list[Any]:  # `pipelines` may be live (agentline.live): read it now
        return [p for p in current(pipelines) if _is_jira_assign(p)]

    for p in jira_targets():
        unknown = set(p.spec.trigger.filter) - JIRA_FILTER_KEYS
        if unknown:
            raise ValidationError(
                f"pipeline {p.name!r}: unknown trigger.filter keys {sorted(unknown)}"
                f" (known: {sorted(JIRA_FILTER_KEYS)})"
            )
    with app.ledger.tx() as tx:
        ensure_table(tx)

    api = FastAPI(title="agentline", docs_url=None, redoc_url=None, openapi_url=None)
    api.state.agentline = app

    @api.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.post("/webhooks/github")
    async def github_webhook(request: Request) -> JSONResponse:
        body = await request.body()
        if not verify_github_signature(
            github_secret, body, request.headers.get("x-hub-signature-256")
        ):
            return _unauthorized("bad or missing X-Hub-Signature-256")
        event = request.headers.get("x-github-event", "")
        delivery = request.headers.get("x-github-delivery")
        if event == "ping":
            return JSONResponse({"status": "pong"})
        if event != "pull_request_review":
            return _ignored(f"event {event!r}")
        if not delivery:
            return JSONResponse({"status": "error", "reason": "no X-GitHub-Delivery"}, 400)
        try:
            data = json.loads(body)
        except ValueError:
            return JSONResponse({"status": "error", "reason": "body is not JSON"}, 400)
        review = data.get("review") or {}
        state = str(review.get("state") or "").lower()
        if data.get("action") != "submitted":
            return _ignored(f"review action {data.get('action')!r}")
        if state not in ("approved", "changes_requested"):
            return _ignored(f"review state {state!r} does not resume a wait")
        repo = (data.get("repository") or {}).get("full_name")
        number = (data.get("pull_request") or {}).get("number")
        if not repo or not number:
            return _ignored("no repository/pull_request in payload")
        comments: list[dict[str, Any]] = []
        client = github_client()
        if client is not None and review.get("id"):
            try:
                comments = await asyncio.to_thread(
                    client.list_review_comments, repo, int(number), int(review["id"])
                )
            except Exception as e:  # the review body alone still resumes the wait
                log.warning("could not fetch review comments for %s#%s: %s", repo, number, e)
        payload = review_payload(repo, int(number), review, comments, source="webhook")
        key = review_key(repo, int(number))

        def deliver() -> tuple[str, list[str]]:
            with app.ledger.tx() as tx:
                if not claim_delivery(tx, "github", delivery):
                    return "replay", []
                woken = deliver_once(app, tx, key, payload)
                if woken is None:
                    return "duplicate", []
                runs = [x.run_id for i in woken if (x := tx.get_execution(i)) is not None]
                return "accepted", runs

        outcome, run_ids = await asyncio.to_thread(deliver)
        if outcome == "replay":
            return JSONResponse({"status": "duplicate", "delivery": delivery}, 409)
        if outcome == "duplicate":  # the poller already delivered this review
            return JSONResponse({"status": "duplicate", "event_id": payload["event_id"]}, 200)
        return JSONResponse({"status": "accepted", "correlation_key": key, "run_ids": run_ids}, 202)

    @api.post("/webhooks/jira")
    async def jira_webhook(request: Request) -> JSONResponse:
        given = request.headers.get("x-agentline-secret") or request.query_params.get("secret")
        if not verify_shared_secret(jira_secret, given):
            return _unauthorized("bad or missing shared secret")
        try:
            data = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"status": "error", "reason": "body is not JSON"}, 400)
        a = jira_assignment(data) if isinstance(data, dict) else None
        if a is None:
            return _ignored("not an issue assignment")
        jira_pipelines = jira_targets()
        matched = [p for p in jira_pipelines if filter_matches(p.spec.trigger.filter, a)]
        if not matched:
            return _ignored("no pipeline trigger matches")
        base = f"jira:{a['issue_key']}:assigned:{a['assignee']}:{a['marker']}"
        payload = {
            "issue_key": a["issue_key"],
            "assignee": a["assignee"],
            "event": "issue_assigned",
        }
        runs = []
        for p in matched:
            dedupe = base if len(jira_pipelines) == 1 else f"{base}:{p.name}"
            sub = await app.submit(p.name, payload, dedupe_key=dedupe)
            runs.append({"pipeline": p.name, "run_id": sub.run_id, "created": sub.created})
        if not any(r["created"] for r in runs):
            return JSONResponse({"status": "duplicate", "runs": runs}, 200)
        return JSONResponse(
            {"status": "accepted", "run_ids": [r["run_id"] for r in runs], "runs": runs}, 202
        )

    return api
