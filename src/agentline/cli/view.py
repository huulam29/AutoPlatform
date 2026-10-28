"""`runs list`, `runs show` and `audit`: one dict view per run (for --json) and its text form."""

from __future__ import annotations

import json
import time
from typing import Any

from agentline.app import App
from agentline.cli.proposal import proposal_id
from agentline.errors import ValidationError
from agentline.ledger.base import AuditEntry, Execution, Run

EXCERPT_MAX = 160


def ts(at: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) if at else "-"


def money(v: float | None) -> str:
    return "-" if v is None else f"${v:.2f}"


def clip(v: Any, n: int = EXCERPT_MAX) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str, sort_keys=True)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def run_dict(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "pipeline": run.flow,
        "status": run.status,
        "cost_usd": round(run.cost_usd, 6),
        "budget_usd": run.budget_usd,
        "dedupe_key": run.dedupe_key,
        "error": run.error,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def detail_excerpt(ex: Execution) -> dict[str, Any]:
    """The parts of `detail` a human needs to decide: missing tools + image proposal, gate
    prompt/approvers, wait key/waiter, unknown effect + advice, budget, loop, image used."""
    d = ex.detail or {}
    out: dict[str, Any] = {}
    for k in ("kind", "missing_tools", "prompt", "approvers", "waiter", "poll_every"):
        if d.get(k) not in (None, [], ""):
            out[k] = d[k]
    if ex.status == "waiting" and ex.correlation_key:
        out["wait_key"] = ex.correlation_key
    for k in ("effect", "description", "advice", "spent_usd", "budget_usd", "scope"):
        if d.get(k) not in (None, ""):
            out[k] = d[k]
    if "edge" in d:
        out["loop"] = {"edge": d["edge"], "max_loops": d.get("max_loops")}
    if isinstance(d.get("image"), dict):
        img = d["image"]
        out["image"] = {k: img.get(k) for k in ("ref", "profile", "source") if img.get(k)}
    if isinstance(d.get("image_proposal"), dict):
        out["image_proposal"] = {"id": proposal_id(ex), **d["image_proposal"]}
    if isinstance(d.get("deploy"), dict):
        out["deploy"] = d["deploy"]
    if isinstance(d.get("attachments"), dict):
        out["attachments"] = sorted(d["attachments"])
    return out


def exec_dict(ex: Execution) -> dict[str, Any]:
    return {
        "key": ex.key,
        "id": ex.id,
        "node_id": ex.node_id,
        "iteration": ex.iteration,
        "status": ex.status,
        "attempt": ex.attempt,
        "reclaims": ex.reclaims,
        "cost_usd": round(ex.cost_usd, 6),
        "reason": ex.reason,
        "error": ex.error,
        "detail": detail_excerpt(ex),
    }


def audit_dict(a: AuditEntry) -> dict[str, Any]:
    return {
        "id": a.id,
        "at": a.at,
        "actor": a.actor,
        "action": a.action,
        "run_id": a.run_id,
        "node_id": a.node_id,
        "detail": a.detail,
    }


def next_steps(run: Run, execs: list[Execution]) -> list[str]:
    """Exact commands for what's waiting on a human."""
    rid = run.id
    out: list[str] = []
    for ex in execs:
        node = ex.node_id
        if ex.status == "needs-review":
            pid = proposal_id(ex) if ex.reason == "missing_tools" else None
            if pid:
                out.append(
                    f"agentline approve {rid} --proposal {pid}   (apply, build, retry {ex.key})"
                )
                out.append(f"agentline resolve {rid} --node {node} --action skip|fail   (decline)")
            elif ex.reason == "effect_unknown":
                out.append(
                    f"agentline resolve {rid} --node {node} --action retry --effect redo|done"
                    " [--effect-result JSON]   (did the effect happen?)"
                )
            else:
                out.append(
                    f"agentline resolve {rid} --node {node} --action retry|skip|fail|done"
                    " [--result JSON]"
                )
        elif ex.status == "waiting" and (ex.detail or {}).get("kind") == "gate":
            out.append(f"agentline approve {rid} --node {node} [--comment C]")
            out.append(f"agentline reject {rid} --node {node} [--comment C]")
        elif ex.status == "waiting":
            out.append(f"(waiting for event {ex.correlation_key} on {ex.key})")
    if run.status == "dead":
        out.append(f"agentline runs retry {rid}")
    return out


def run_view(app: App, run_id: str) -> dict[str, Any]:
    with app.ledger.tx() as tx:
        run = tx.get_run(run_id)
        if run is None:
            raise ValidationError(f"no run {run_id}")
        execs = tx.list_executions(run_id)
        effects = tx.list_effects(run_id)
        audit = tx.list_audit(run_id)
    lineage = [x for x in app.lineage(run_id) if x["reads"] or x["writes"]]
    return {
        "run": run_dict(run),
        "executions": [exec_dict(x) for x in execs],
        "effects": [
            {
                "node": f"{e.node_id}#{e.iteration}",
                "name": e.name,
                "status": e.status,
                "key": e.idempotency_key,
                "advice": e.advice,
            }
            for e in effects
        ],
        "lineage": [
            {
                "node": x["node"],
                "reads": [
                    {k: r.get(k) for k in ("name", "from", "summary", "hash")} for r in x["reads"]
                ],
                "writes": [
                    {k: w.get(k) for k in ("name", "summary", "size", "hash")} for w in x["writes"]
                ],
            }
            for x in lineage
        ],
        "audit": [audit_dict(a) for a in audit],
        "next": next_steps(run, execs),
    }


# -- text -------------------------------------------------------------------------------------
def render_runs(runs: list[Run]) -> str:
    if not runs:
        return "no runs"
    rows = [("RUN", "PIPELINE", "STATUS", "COST", "CREATED", "DEDUPE")]
    rows += [
        (
            r.id,
            r.flow,
            r.status,
            money(r.cost_usd) + (f"/{money(r.budget_usd)}" if r.budget_usd is not None else ""),
            ts(r.created_at),
            r.dedupe_key or "",
        )
        for r in runs
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in rows
    )


def _proposal_lines(p: dict[str, Any]) -> list[str]:
    lines = [f"    image proposal  {p.get('id')}  (by {p.get('proposer', '?')})"]
    lines.append(f"      profile {p.get('profile_from')} → {p.get('profile_to')}")
    if p.get("add_apt"):
        lines.append(f"        + apt: {', '.join(p['add_apt'])}")
    if p.get("reason"):
        lines.append(f"      reason: {clip(p['reason'])}")
    if p.get("unknown"):
        lines.append(f"      no package known for: {', '.join(p['unknown'])}")
    return lines


def render_run(v: dict[str, Any]) -> str:
    r = v["run"]
    lines = [f"run {r['id']}  {r['pipeline']}  {r['status']}"]
    budget = f" / {money(r['budget_usd'])}" if r["budget_usd"] is not None else ""
    meta = f"  cost {money(r['cost_usd'])}{budget}"
    if r["dedupe_key"]:
        meta += f"   dedupe: {r['dedupe_key']}"
    lines.append(meta + f"   created {ts(r['created_at'])}")
    if r["error"]:
        lines.append(f"  error: {clip(r['error'], 300)}")
    lines.append("executions")
    for x in v["executions"]:
        head = (
            f"  {x['key']:<16} {x['status']:<13} attempt {x['attempt']}"
            + (f"  reclaims {x['reclaims']}" if x["reclaims"] else "")
            + (f"  {money(x['cost_usd'])}" if x["cost_usd"] else "")
            + (f"  reason: {x['reason']}" if x["reason"] else "")
        )
        lines.append(head)
        if x["error"] and x["error"] != x["reason"]:
            lines.append(f"      error:    {clip(x['error'])}")
        d = x["detail"]
        if d.get("missing_tools"):
            lines.append(f"      missing:  {', '.join(d['missing_tools'])}")
        if d.get("image"):
            img = d["image"]
            prof = f" (profile: {img['profile']})" if img.get("profile") else ""
            lines.append(f"      image:    {img.get('ref')}{prof}")
        if d.get("kind") == "gate" and x["status"] == "waiting":
            lines.append(f"      gate:     {clip(d.get('prompt', ''))}")
            if d.get("approvers"):
                lines.append(f"      approvers: {', '.join(d['approvers'])}")
        if d.get("wait_key") and d.get("kind") != "gate":
            via = f" via {d['waiter']}" if d.get("waiter") else ""
            lines.append(f"      waiting:  {d['wait_key']}{via}")
        if d.get("effect"):
            lines.append(f"      effect:   {d['effect']}  {clip(d.get('description', ''))}")
        if d.get("advice"):
            lines.append(f"      advice:   {clip(d['advice'])}")
        if d.get("scope"):
            lines.append(
                f"      budget:   {d['scope']} spent {money(d.get('spent_usd'))}"
                f" of {money(d.get('budget_usd'))}"
            )
        if d.get("image_proposal"):
            lines += _proposal_lines(d["image_proposal"])
    if v["effects"]:
        lines.append("effects")
        for e in v["effects"]:
            extra = f"  key={e['key']}" if e["key"] else ""
            if e["advice"]:
                extra += f"  advice={clip(e['advice'], 80)}"
            lines.append(f"  {e['node']:<16} {e['name']:<20} {e['status']}{extra}")
    if v["lineage"]:
        lines.append("lineage")
        for x in v["lineage"]:
            for rd in x["reads"]:
                src = f" (from {rd['from']})" if rd.get("from") else ""
                lines.append(
                    f"  {x['node']:<16} read  {rd['name']}{src}: {clip(rd['summary'], 90)}"
                )
            for w in x["writes"]:
                lines.append(f"  {x['node']:<16} wrote {w['name']}: {clip(w['summary'], 90)}")
    if v["audit"]:
        lines.append("audit")
        lines += ["  " + render_audit_line(a) for a in v["audit"]]
    if v["next"]:
        lines.append("next: " + v["next"][0])
        lines += ["      " + n for n in v["next"][1:]]
    return "\n".join(lines)


def render_audit_line(a: dict[str, Any]) -> str:
    node = f" {a['node_id']}" if a.get("node_id") else ""
    d = {k: x for k, x in (a.get("detail") or {}).items() if k != "diff" and x not in (None, "")}
    return f"{ts(a['at'])}  {a['actor']}  {a['action']}  {a.get('run_id') or '-'}{node}  {clip(d)}"
