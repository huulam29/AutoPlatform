"""A run as one document to read, share or attach (phase 58): JSON (`build`) and Markdown
(`markdown`, a pure function of the JSON). Everything is redacted; outputs appear only as their
short artifact summaries, and the timeline is capped at MAX_TIMELINE lines."""

from __future__ import annotations

import contextlib
import time
from datetime import UTC, datetime
from typing import Any

from agentline import feedback as fb
from agentline import metrics

MAX_TIMELINE = 300
MAX_ERROR = 1500


def _summary(app: Any, tx: Any, digest: str | None) -> str:
    if not digest:
        return ""
    with contextlib.suppress(Exception):
        meta = app.scheduler.artifacts.meta(tx, digest)
        if meta is not None:
            return str(meta.summary or "")[:300]
    return ""


def build(
    app: Any,
    run_id: str,
    *,
    review_store: Any = None,
    triage_store: Any = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """The run's report (None: no such run)."""
    from agentline import review as rv
    from agentline.triage import TriageStore

    now = time.time() if now is None else now
    m = metrics.run_metrics(app, run_id, now=now)
    if m is None:
        return None
    tstore = triage_store if triage_store is not None else TriageStore()
    with app.ledger.tx() as tx:
        run = tx.get_run(run_id)
        assert run is not None
        execs = sorted(tx.list_executions(run_id), key=lambda x: (x.created_at, x.iteration))
        journal = tx.list_journal(run_id)
        outputs = [
            {
                "node": x.node_id,
                "iteration": x.iteration,
                "status": x.status,
                "attempt": x.attempt,
                "summary": _summary(app, tx, x.result_ref) if x.status == "done" else "",
                "error": str(x.error or "")[:MAX_ERROR],
                "reason": x.reason or "",
            }
            for x in execs
        ]
        rows = [fb._entry(j) for j in journal if j.kind == fb.KIND]
    sig = None
    with contextlib.suppress(Exception):
        sig = rv.signals(app, run_id, triage_store=tstore)
    review = None
    with contextlib.suppress(Exception):
        rec = (review_store or rv.ReviewStore()).get(rv.review_id(run_id))
        if rec is not None:
            review = {k: rec.get(k) for k in ("id", "status", "summary", "keep", "rows", "lesson")}
            review["proposals"] = [
                {"type": a.get("type"), "summary": a.get("summary"), "node": a.get("node")}
                for a in rec.get("actions") or []
            ]
    timeline = [
        {
            "at": j.at,
            "node": j.node_id,
            "iteration": j.iteration,
            "task": j.task,
            "kind": j.kind,
            "message": j.message,
        }
        for j in journal
    ]
    omitted = max(0, len(timeline) - MAX_TIMELINE)
    report = {
        "generated_at": now,
        "run": {
            "id": run.id,
            "pipeline": run.flow,
            "status": run.status,
            "created_at": run.created_at,
            "ended_at": m["ended_at"],
            "error": str(run.error or "")[:MAX_ERROR],
            "budget_usd": run.budget_usd,
            "cost_usd": m["cost_usd"],
        },
        "metrics": m,
        "signals": rv.signal_lines(sig) if sig else [],
        "feedback": {"entries": rows, "summary": fb.summarize(rows)},
        "triage": [
            {k: t.get(k) for k in ("node", "verdict", "status", "summary")}
            for t in tstore.list(run_id)
        ],
        "review": review,
        "outputs": outputs,
        "timeline": timeline[:MAX_TIMELINE],
        "timeline_omitted": omitted,
    }
    return app.redactor.redact_value(report)  # type: ignore[no-any-return]


def _ts(t: float | None) -> str:
    if not t:
        return "-"
    return datetime.fromtimestamp(float(t), UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _dur(s: float) -> str:
    s = float(s or 0)
    if s >= 3600:
        return f"{s / 3600:.1f} h"
    if s >= 90:
        return f"{s / 60:.1f} min"
    return f"{s:.0f} s"


def _tok(t: dict[str, int] | None) -> str:
    if not t:
        return "-"
    return f"{t.get('input', 0):,} in / {t.get('output', 0):,} out"


def _cell(v: Any) -> str:
    return str(v if v not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def _row(*cells: Any) -> str:
    return "| " + " | ".join(_cell(v) for v in cells) + " |"


def markdown(rep: dict[str, Any]) -> str:
    r = rep["run"]
    m = rep["metrics"]
    t = m["totals"]
    out = [
        f"# Run {r['id']} of {r['pipeline']}",
        "",
        f"- Status: **{r['status']}**",
        f"- Started: {_ts(r['created_at'])}; ended: {_ts(r['ended_at'])}",
        f"- Wall time: {_dur(m['wall_s'])} (work {_dur(t['work_s'])}, queued"
        f" {_dur(t['queued_s'])}, waiting {_dur(t['waiting_s'])})",
        f"- Cost: ${r['cost_usd']:.4f}"
        + (f" of a ${r['budget_usd']:.2f} budget" if r.get("budget_usd") is not None else ""),
        f"- Tokens: {_tok(t['tokens'])}; turns: {_cell(t['turns'])}",
        f"- Retries: {t['retries']}; feedback: {m['feedback']['up']} 👍,"
        f" {m['feedback']['down']} 👎",
    ]
    if r.get("error"):
        out += ["", "## Error", "", "```text", r["error"], "```"]
    out += [
        "",
        "## Nodes",
        "",
        "| Node | Kind | Runs | Retries | Work | Waiting | Cost | Tokens | Turns | 👍 | 👎 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for n in m["nodes"]:
        out.append(
            _row(
                n["node"],
                n["kind"],
                n["executions"],
                n["retries"],
                _dur(n["work_s"]),
                _dur(n["waiting_s"]),
                f"${n['cost_usd']:.4f}",
                _tok(n["tokens"]),
                n["turns"],
                n["feedback"]["up"],
                n["feedback"]["down"],
            )
        )
    out += ["", "## Signals", ""]
    out += [f"- {s}" for s in rep["signals"]] or ["- none"]
    out += ["", "## Feedback", ""]
    out += [f"- {s}" for s in fb.text(rep["feedback"]["entries"], 200)] or ["- none"]
    if rep["triage"]:
        out += ["", "## Failure triage", ""]
        out += [
            f"- {x.get('node') or 'the run'}: {x.get('verdict') or x.get('status')}:"
            f" {x.get('summary') or ''}"
            for x in rep["triage"]
        ]
    rv = rep.get("review")
    if rv:
        out += ["", "## Review", "", f"Status: {rv.get('status')}. {rv.get('summary') or ''}"]
        for row in rv.get("rows") or []:
            out.append(
                f"- **{row.get('node')}** ({row.get('component')}, {row.get('verdict')}):"
                f" {row.get('observed')}"
            )
        for p in rv.get("proposals") or []:
            out.append(f"- Proposal ({p.get('type')}): {p.get('summary') or ''}")
        if rv.get("lesson"):
            out.append(f"- Lesson: {(rv['lesson'] or {}).get('lesson')}")
    out += ["", "## Outputs", "", "| Node | Iteration | Status | Attempt | Summary / error |",
            "|---|---|---|---|---|"]  # fmt: skip
    for o in rep["outputs"]:
        out.append(
            _row(
                o["node"],
                o["iteration"],
                o["status"],
                o["attempt"],
                (o["summary"] or o["error"] or o["reason"])[:300],
            )
        )
    out += ["", "## Timeline", ""]
    for j in rep["timeline"]:
        where = f"{j['node']} #{j['iteration']}" if j["node"] else "run"
        if j.get("task") is not None:
            where += f" task {j['task']}"
        out.append(f"- {_ts(j['at'])} {where}: {j['message']}")
    if rep["timeline_omitted"]:
        out.append(f"- … {rep['timeline_omitted']} more line(s) left out")
    out += ["", f"_Generated {_ts(rep['generated_at'])} by agentline. Secrets are redacted._", ""]
    return "\n".join(out)
