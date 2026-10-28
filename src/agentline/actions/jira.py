"""jira.get_ticket and jira.comment."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentline.actions._util import require
from agentline.context import StepContext
from agentline.integrations.jira import JiraClient
from agentline.registry import ActionCall

JiraFactory = Callable[[], JiraClient]


def comment_marker(ctx: StepContext) -> str:
    return f"[agentline:{ctx.run_id}:{ctx.node_id}#{ctx.iteration}]"


def _issue_key(call: ActionCall, ctx: StepContext) -> str:
    key = call.params.get("key")
    if not key:
        ticket = call.inputs.get("ticket")
        key = ticket.get("key") if isinstance(ticket, dict) else None
    if not key and isinstance(ctx.payload, dict):
        key = ctx.payload.get("issue_key")
    return str(require({"key": key}, "key", "jira"))


@dataclass
class GetTicket:
    """Read the issue. Read-only: no effect guard (a repeat is harmless).

    Params: `key` (defaults to the `ticket` input's key or the run payload's `issue_key`).
    Returns `{key, summary, description (plain text), assignee, status, labels, project, url}`.
    """

    client: JiraFactory = JiraClient.from_env
    name: str = "jira.get_ticket"
    deploy_class: bool = False

    async def __call__(self, ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        key = _issue_key(call, ctx)
        jira = self.client()
        return await asyncio.to_thread(jira.get_issue, key)


@dataclass
class Comment:
    """Add a comment. Effect strategy: **check**.

    The body ends with a marker `[agentline:<run_id>:<node>#<iteration>]`; after a crash the
    check lists the issue's comments and looks for that marker, so a comment is never posted
    twice by the same node iteration. Jira has no hidden text, so the marker is a visible last
    line. Params: `key` (as jira.get_ticket), `body`. Returns `{id, key, marker}`.
    """

    client: JiraFactory = JiraClient.from_env
    name: str = "jira.comment"
    deploy_class: bool = False

    async def __call__(self, ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        key = _issue_key(call, ctx)
        body = str(require(call.params, "body", self.name))
        marker = comment_marker(ctx)
        jira = self.client()

        async def check() -> dict[str, Any] | None:
            comments = await asyncio.to_thread(jira.list_comments, key)
            for c in comments:
                if marker in c["body"]:
                    return {"id": c["id"], "key": key, "marker": marker}
            return None

        async def do() -> dict[str, Any]:
            created = await asyncio.to_thread(jira.add_comment, key, f"{body}\n\n{marker}")
            return {"id": created["id"], "key": key, "marker": marker}

        return await ctx.effect(
            "comment",
            do,
            check=check,
            key=call.effect.key,
            on_unknown=call.effect.on_unknown or "halt",
            description=f"comment on Jira issue {key}",
        )
