"""Jira polling: start runs for tickets newly assigned to you, without webhooks.

For a laptop (the Mac app) that Jira can't reach:

    trigger: { type: poll, source: jira, event: issue_assigned, every: 5m,
               filter: { project: PROJ } }      # or filter: { jql: "..." } to replace the query

Every `every` seconds the loop searches Jira for tickets assigned to the token's account and
starts a run for each assignment made **after polling was first enabled** for that pipeline
(the start time is kept in `$AGENTLINE_HOME/state/jira_poll.json`), so turning it on doesn't
start runs for your whole backlog. The dedupe key matches the webhook's,
`jira:<key>:assigned:<accountId>:<changelog id>`, so a ticket is never started twice even if
both webhooks and polling are on, and a reassignment back to you is a new run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentline.app import App
from agentline.integrations.jira import JiraClient

log = logging.getLogger("agentline.jira_poll")


def poll_pipelines(pipelines: list[Any]) -> list[Any]:
    return [
        p for p in pipelines if p.spec.trigger.type == "poll" and p.spec.trigger.source == "jira"
    ]


def state_path() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "state" / "jira_poll.json"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _parse_time(text: str) -> float | None:
    """Jira timestamps look like 2026-10-19T12:00:00.000+0200."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def assignment(issue: dict[str, Any], account: str) -> tuple[str, float] | None:
    """(changelog history id, time) of the latest change that assigned `issue` to `account`."""
    best: tuple[str, float] | None = None
    for h in (issue.get("changelog") or {}).get("histories") or []:
        for item in h.get("items") or []:
            if item.get("field") == "assignee" and item.get("to") == account:
                at = _parse_time(str(h.get("created") or ""))
                if at is not None and (best is None or at > best[1]):
                    best = (str(h.get("id")), at)
    return best


def build_jql(trigger: Any, since: float) -> str:
    if trigger.filter.get("jql"):
        return str(trigger.filter["jql"])
    # Coarse prefilter (JQL dates are in the Jira user's timezone; the changelog check below
    # is exact), so look back a day before the start time.
    day = datetime.fromtimestamp(since - 86400, UTC).strftime("%Y/%m/%d %H:%M")
    jql = f'assignee = currentUser() AND assignee CHANGED TO currentUser() AFTER "{day}"'
    if trigger.filter.get("project"):
        jql = f'project = "{trigger.filter["project"]}" AND ' + jql
    return jql + " ORDER BY updated DESC"


async def poll_once(
    app: App,
    pipeline: Any,
    client: Any,
    *,
    account: str,
    since: float,
) -> list[str]:
    """One search; returns the ids of runs created."""
    jql = build_jql(pipeline.spec.trigger, since)
    issues = await asyncio.to_thread(client.search, jql)
    created = []
    for issue in issues:
        found = assignment(issue, account)
        if found is None or found[1] < since:
            continue  # assigned before polling was enabled, or not by a visible change
        history_id, _ = found
        key = str(issue.get("key"))
        dedupe = f"jira:{key}:assigned:{account}:{history_id}"
        payload = {"issue_key": key, "assignee": account, "event": "issue_assigned"}
        sub = await app.submit(pipeline.name, payload, dedupe_key=dedupe)
        if sub.created:
            log.info("jira poll: %s assigned to you -> run %s (%s)", key, sub.run_id, pipeline.name)
            created.append(sub.run_id)
    return created


async def run_jira_poll(
    app: App,
    pipelines: list[Any],
    stop: asyncio.Event,
    *,
    client_factory: Callable[[], Any] = JiraClient.from_env,
    clock: Callable[[], float] = time.time,
    state_file: Path | None = None,
) -> None:
    from agentline.live import LivePipelines, current, version

    live = isinstance(pipelines, LivePipelines)
    targets = poll_pipelines(current(pipelines))
    seen = version(pipelines)
    status: dict[str, Any] = {
        "enabled": bool(targets),
        "every_s": min((p.spec.trigger.every for p in targets), default=None),
        "pipelines": [p.name for p in targets],
        "last_poll_at": None,
        "last_error": None,
        "runs_started": 0,
    }
    app.runtime["jira_poll"] = status
    if not targets and not live:
        return
    path = state_file or state_path()
    state = _load_state(path)
    for p in targets:
        state.setdefault(p.name, {"since": clock()})
    _save_state(path, state)
    next_at = dict.fromkeys((p.name for p in targets), 0.0)
    account: str | None = None
    client: Any = None
    while not stop.is_set():
        if version(pipelines) != seen:  # a live reload: follow added / removed poll triggers
            seen = version(pipelines)
            targets = poll_pipelines(current(pipelines))
            for p in targets:
                if p.name not in state:
                    state[p.name] = {"since": clock()}
                    _save_state(path, state)
                next_at.setdefault(p.name, 0.0)
            status.update(
                enabled=bool(targets),
                every_s=min((p.spec.trigger.every for p in targets), default=None),
                pipelines=[p.name for p in targets],
            )
        now = clock()
        due = [p for p in targets if now >= next_at[p.name]]
        for p in due:
            next_at[p.name] = now + p.spec.trigger.every
            try:
                if client is None:
                    client = client_factory()
                if account is None:
                    me = await asyncio.to_thread(client.myself)
                    account = str(me.get("accountId") or me.get("name") or "")
                runs = await poll_once(
                    app, p, client, account=account, since=float(state[p.name]["since"])
                )
                status["runs_started"] += len(runs)
                status["last_error"] = None
            except Exception as e:  # keep polling; the app shows the error
                status["last_error"] = f"{p.name}: {type(e).__name__}: {e}"
                log.warning("jira poll failed: %s", status["last_error"])
                client = None if "credential" in str(e).lower() else client
            status["last_poll_at"] = clock()
        pending = [next_at[p.name] for p in targets]
        wait = max(1.0, min(pending) - clock()) if pending else 1.0
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), wait)
