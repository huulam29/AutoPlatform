"""'Fix this': turn a problem (an issue, a failed run) into a request for the editor assistant.

The request carries what a person would look at first: the error, the traceback or the failed
nodes with their journal and transcript tail, and which pipeline it belongs to. The assistant
then proposes a fix (a pipeline change, a setting, a script) and, when it makes sense, a
`retry` of the failed node, all as cards the user applies.
"""

from __future__ import annotations

from typing import Any

MAX_TRANSCRIPT = 4000


def _transcript_tail(app: Any, x: Any) -> str:
    atts = (x.detail or {}).get("attachments") or {}
    if not atts:
        return ""
    name = sorted(atts)[-1]  # the last attempt's transcript
    digest = atts[name].get("ref")
    if not digest:
        return ""
    try:
        with app.ledger.tx() as tx:
            data = app.scheduler.artifacts.read_bytes(tx, digest) or b""
    except Exception:  # an artifact on disk that's gone: not worth failing over
        return ""
    text = data.decode("utf-8", errors="replace")
    return f"\n--- {name} (last {MAX_TRANSCRIPT} chars) ---\n{text[-MAX_TRANSCRIPT:]}"


def for_run(app: Any, run_id: str) -> dict[str, Any] | None:
    with app.ledger.tx() as tx:
        run = tx.get_run(run_id)
        if run is None:
            return None
        execs = tx.list_executions(run_id)
        journal = tx.list_journal(run_id)
        tasks = tx.list_tasks(run_id)
    failed = [x for x in execs if x.status in ("dead", "needs-review", "failed")]
    lines = [
        f"Run {run.id} of pipeline {run.flow} ended {run.status}.",
        f"Run error: {run.error}" if run.error else "",
    ]
    for x in failed:
        lines.append(f"\n## Node {x.node_id} (iteration {x.iteration}): {x.status}")
        if x.reason:
            lines.append(f"Reason: {x.reason}")
        if x.error:
            lines.append(f"Error: {x.error[:3000]}")
        bad = [t for t in tasks if t.execution_id == x.id and t.status == "failed"]
        for t in bad[:10]:
            lines.append(f"- task {t.idx} (item {str(t.item)[:200]}): {t.error}")
        hist = [j for j in journal if j.node_id == x.node_id][-15:]
        if hist:
            lines.append("Journal:")
            for j in hist:
                err = j.detail.get("error")
                lines.append(f"  {j.message}" + (f" ({err})" if err else ""))
        lines.append(_transcript_tail(app, x))
    return {"flow": run.flow, "run": run.id, "text": "\n".join(x for x in lines if x)}


def request_text(problem: str, run_id: str | None) -> str:
    ask = (
        "Something failed. Find the cause from what's below, then propose the fix as actions: a"
        " pipeline change (prompt, schema, script, node settings), a setting, or a key to set."
    )
    if run_id:
        ask += (
            f" If the fix makes it worth trying again, also propose a retry action for run"
            f" {run_id} (and the failed node), and say why."
        )
    return f"{ask}\n\n{problem}"


def for_issue(app: Any, issue: dict[str, Any], incident: dict[str, Any] | None) -> dict[str, Any]:
    src = issue.get("source") or {}
    head = f"Issue #{issue['number']}: {issue.get('title', '')} (status {issue.get('status')})"
    if src.get("kind") == "run":
        found = for_run(app, str(src.get("ref")))
        if found:
            return {
                "flow": found["flow"],
                "run": found["run"],
                "prompt": request_text(f"{head}\n\n{found['text']}", found["run"]),
            }
    body = [head]
    if incident:
        body += [
            f"Kind: {incident.get('kind')}  Seen {incident.get('count', 1)}x",
            str(incident.get("message") or "")[:3000],
        ]
        if incident.get("traceback"):
            body.append("Traceback:\n" + str(incident["traceback"])[-4000:])
        ctx = incident.get("context") or {}
        run_id = ctx.get("run_id") if isinstance(ctx, dict) else None
        if run_id and (found := for_run(app, str(run_id))):
            body.append(found["text"])
            return {"flow": found["flow"], "run": found["run"],
                    "prompt": request_text("\n\n".join(body), found["run"])}  # fmt: skip
    elif issue.get("detail"):
        body.append(str(issue["detail"]))
    return {"flow": None, "run": None, "prompt": request_text("\n\n".join(body), None)}
