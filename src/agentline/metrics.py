"""Run metrics and trends, computed on read from what the ledger already holds (phase 58).

- `run_metrics(app, run_id)`: per node and for the run: work / queued / waiting time (from the
  journal's status rows: time in `running`, in `ready` / `failed`, in `waiting`), attempts,
  retries, iterations, cost, tokens and turns where the runner reported them (`runner_meta.*` in
  the execution's detail), the models used and people's ratings.
- `insights(app, days, pipeline)`: finished runs per pipeline, per day and (for one pipeline) per
  node, from runs + executions + feedback rows only (no artifact reads).

"Tool calls" are the runner's reported turns; no runner reports single tool calls.
"""

from __future__ import annotations

import contextlib
import statistics
import time
from datetime import UTC, datetime
from typing import Any

from agentline import feedback as fb

FINISHED = ("done", "dead", "cancelled")
INSIGHT_STATUSES = ("done", "dead", "needs-review")
MAX_INSIGHT_RUNS = 2000
_TOKEN_KEYS = {
    "input": ("input_tokens", "prompt_tokens"),
    "output": ("output_tokens", "completion_tokens"),
    "cache_read": ("cache_read_input_tokens", "cached_input_tokens"),
    "cache_write": ("cache_creation_input_tokens",),
}
_BUCKET = {"running": "work_s", "waiting": "waiting_s", "ready": "queued_s", "failed": "queued_s"}


def _dst(message: str) -> str | None:
    """The status a journal status row moved to: 'created (ready)', 'a -> b'."""
    if " -> " in message:
        return message.rsplit(" -> ", 1)[1].strip()
    if message.startswith("created (") and message.endswith(")"):
        return message[len("created (") : -1]
    return None


def tokens_of(detail: dict[str, Any]) -> dict[str, int] | None:
    """Sum of the token usage every runner call of an execution reported; None if none did."""
    out = {k: 0 for k in _TOKEN_KEYS}
    seen = False
    for key, meta in (detail or {}).items():
        if not key.startswith("runner_meta.") or not isinstance(meta, dict):
            continue
        usage = meta.get("usage")
        if not isinstance(usage, dict):
            continue
        for name, keys in _TOKEN_KEYS.items():
            for k in keys:
                v = usage.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[name] += int(v)
                    seen = True
                    break
    return out if seen else None


def turns_of(detail: dict[str, Any]) -> int | None:
    n = None
    for key, meta in (detail or {}).items():
        if key.startswith("runner_meta.") and isinstance(meta, dict):
            v = meta.get("turns")
            if isinstance(v, int) and not isinstance(v, bool):
                n = (n or 0) + v
    return n


def models_of(detail: dict[str, Any]) -> list[str]:
    out = []
    m = (detail or {}).get("model")
    if isinstance(m, dict) and m.get("name"):
        out.append(str(m["name"]))
    for key, meta in (detail or {}).items():
        if key.startswith("runner_meta.") and isinstance(meta, dict) and meta.get("model"):
            out.append(str(meta["model"]))
    return sorted(set(out))


def _times(rows: list[Any], end: float) -> dict[str, float]:
    """Seconds in each bucket from one execution's status rows (oldest first)."""
    out = {"work_s": 0.0, "queued_s": 0.0, "waiting_s": 0.0}
    cur: str | None = None
    since = 0.0
    for j in rows:
        dst = _dst(j.message)
        if dst is None:
            continue
        if cur in _BUCKET:
            out[_BUCKET[cur]] += max(0.0, float(j.at) - since)
        cur, since = dst, float(j.at)
    if cur in _BUCKET:
        out[_BUCKET[cur]] += max(0.0, end - since)
    return out


def _add_tokens(a: dict[str, int] | None, b: dict[str, int] | None) -> dict[str, int] | None:
    if b is None:
        return a
    if a is None:
        return dict(b)
    return {k: a[k] + b[k] for k in a}


def _empty_node(node: str, kind: str) -> dict[str, Any]:
    return {
        "node": node,
        "kind": kind,
        "executions": 0,
        "iterations": 0,
        "attempts": 0,
        "retries": 0,
        "statuses": {},
        "work_s": 0.0,
        "queued_s": 0.0,
        "waiting_s": 0.0,
        "cost_usd": 0.0,
        "tokens": None,
        "turns": None,
        "models": [],
        "feedback": {"up": 0, "down": 0},
    }


def _kind(graph: Any, node: str) -> str:
    with contextlib.suppress(Exception):
        return str(graph.nodes[node].meta.get("type") or graph.nodes[node].kind)
    return ""


def run_metrics(app: Any, run_id: str, *, now: float | None = None) -> dict[str, Any] | None:
    """A run's metrics (None: no such run)."""
    now = time.time() if now is None else now
    with app.ledger.tx() as tx:
        run = tx.get_run(run_id)
        if run is None:
            return None
        execs = sorted(tx.list_executions(run_id), key=lambda x: (x.created_at, x.iteration))
        journal = tx.list_journal(run_id)
        rows = [fb._entry(j) for j in journal if j.kind == fb.KIND]
    graph = None
    with contextlib.suppress(Exception):
        graph = app.graph(run.flow, run)
    finished = run.status in FINISHED
    end = float(run.updated_at) if finished else now
    by_exec: dict[str, list[Any]] = {}
    for j in journal:
        if j.kind == "status" and j.execution_id:
            by_exec.setdefault(j.execution_id, []).append(j)
    nodes: dict[str, dict[str, Any]] = {}
    for x in execs:
        n = nodes.setdefault(x.node_id, _empty_node(x.node_id, _kind(graph, x.node_id)))
        n["executions"] += 1
        n["iterations"] = max(n["iterations"], x.iteration)
        n["attempts"] += x.attempt
        n["retries"] += max(0, x.attempt - 1)
        n["statuses"][x.status] = n["statuses"].get(x.status, 0) + 1
        stop = float(x.finished_at) if x.finished_at else end
        t = _times(sorted(by_exec.get(x.id, []), key=lambda j: (j.at, j.id)), stop)
        if not by_exec.get(x.id) and x.started_at:  # no journal (older runs): best effort
            t["work_s"] = max(0.0, stop - float(x.started_at))
            t["queued_s"] = max(0.0, float(x.started_at) - float(x.created_at))
        for k, v in t.items():
            n[k] += v
        n["cost_usd"] += float(x.cost_usd or 0.0)
        n["tokens"] = _add_tokens(n["tokens"], tokens_of(x.detail))
        turns = turns_of(x.detail)
        if turns is not None:
            n["turns"] = (n["turns"] or 0) + turns
        n["models"] = sorted(set(n["models"]) | set(models_of(x.detail)))
    summ = fb.summarize(rows)
    for node, c in summ["nodes"].items():
        if node in nodes:
            nodes[node]["feedback"] = dict(c)
    totals = _empty_node("", "")
    for n in nodes.values():
        for k in (
            "executions",
            "attempts",
            "retries",
            "work_s",
            "queued_s",
            "waiting_s",
            "cost_usd",
        ):
            totals[k] += n[k]
        totals["iterations"] = max(totals["iterations"], n["iterations"])
        totals["tokens"] = _add_tokens(totals["tokens"], n["tokens"])
        if n["turns"] is not None:
            totals["turns"] = (totals["turns"] or 0) + n["turns"]
        totals["models"] = sorted(set(totals["models"]) | set(n["models"]))
        for s, c in n["statuses"].items():
            totals["statuses"][s] = totals["statuses"].get(s, 0) + c
    for d in (*nodes.values(), totals):
        for k in ("work_s", "queued_s", "waiting_s"):
            d[k] = round(d[k], 3)
        d["cost_usd"] = round(d["cost_usd"], 6)
    totals.pop("node")
    totals.pop("kind")
    totals["feedback"] = {"up": summ["up"], "down": summ["down"]}
    return {
        "run": run.id,
        "flow": run.flow,
        "status": run.status,
        "finished": finished,
        "created_at": run.created_at,
        "ended_at": float(run.updated_at) if finished else None,
        "wall_s": round(max(0.0, end - float(run.created_at)), 3),
        "cost_usd": round(float(run.cost_usd or 0.0), 6),
        "node_cost_usd": totals["cost_usd"],
        "totals": totals,
        "nodes": list(nodes.values()),
        "feedback": {"up": summ["up"], "down": summ["down"], "comments": summ["comments"]},
    }


# -- trends ------------------------------------------------------------------------------------
def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return float(s[i])


def insights(
    app: Any, days: int = 30, pipeline: str | None = None, *, now: float | None = None
) -> dict[str, Any]:
    """Trends of runs that ended (done / dead / needs-review) and were created in the window."""
    now = time.time() if now is None else now
    days = max(1, min(int(days), 366))
    since = now - days * 86400
    per_flow: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    per_node: dict[str, dict[str, Any]] = {}
    with app.ledger.tx() as tx:
        runs = [
            r
            for r in tx.list_runs(None, MAX_INSIGHT_RUNS)
            if r.created_at >= since and r.status in INSIGHT_STATUSES
        ]
        for r in runs:
            if pipeline and r.flow != pipeline:
                continue
            execs = tx.list_executions(r.id)
            rows = [fb._entry(j) for j in tx.list_journal(r.id) if j.kind == fb.KIND]
            summ = fb.summarize(rows)
            dur = max(0.0, float(r.updated_at) - float(r.created_at))
            tok = None
            for x in execs:
                tok = _add_tokens(tok, tokens_of(x.detail))
            graph = None
            with contextlib.suppress(Exception):
                graph = app.graph(r.flow, r)
            top: dict[str, int] = {}
            for x in execs:
                top[x.node_id] = max(top.get(x.node_id, 1), x.iteration)
            f = per_flow.setdefault(
                r.flow,
                {"pipeline": r.flow, "runs": 0, "done": 0, "dead": 0, "needs_review": 0,
                 "durations": [], "cost_usd": 0.0, "tokens": 0, "retries": 0, "loops": 0,
                 "rejections": 0, "up": 0, "down": 0},
            )  # fmt: skip
            f["runs"] += 1
            f[r.status.replace("-", "_")] += 1
            f["durations"].append(dur)
            f["cost_usd"] += float(r.cost_usd or 0.0)
            f["tokens"] += sum((tok or {}).values())
            f["retries"] += sum(max(0, x.attempt - 1) for x in execs)
            if graph is not None:  # loop edges taken, as review.signals counts them
                f["loops"] += sum(tx.loop_count(r.id, e.id) for e in graph.edges if e.loop)
            else:  # the pipeline isn't loaded: the most extra iterations of one node
                f["loops"] += max((i - 1 for i in top.values()), default=0)
            f["rejections"] += sum(
                1 for x in execs if x.status == "dead" and _kind(graph, x.node_id) == "gate"
            )
            f["up"] += summ["up"]
            f["down"] += summ["down"]
            d = by_day.setdefault(
                _day(r.created_at),
                {"runs": 0, "done": 0, "failed": 0, "cost_usd": 0.0, "durations": [],
                 "up": 0, "down": 0},
            )  # fmt: skip
            d["runs"] += 1
            d["done" if r.status == "done" else "failed"] += 1
            d["cost_usd"] += float(r.cost_usd or 0.0)
            d["durations"].append(dur)
            d["up"] += summ["up"]
            d["down"] += summ["down"]
            if pipeline:
                for x in execs:
                    n = per_node.setdefault(
                        x.node_id,
                        {"node": x.node_id, "kind": _kind(graph, x.node_id), "executions": 0,
                         "failures": 0, "durations": [], "cost_usd": 0.0, "retries": 0,
                         "up": 0, "down": 0},
                    )  # fmt: skip
                    n["executions"] += 1
                    if x.status in ("dead", "needs-review"):
                        n["failures"] += 1
                    if x.started_at and x.finished_at:
                        n["durations"].append(max(0.0, x.finished_at - x.started_at))
                    n["cost_usd"] += float(x.cost_usd or 0.0)
                    n["retries"] += max(0, x.attempt - 1)
                for node, c in summ["nodes"].items():
                    if node in per_node:
                        per_node[node]["up"] += c["up"]
                        per_node[node]["down"] += c["down"]
    pipelines = []
    for f in per_flow.values():
        durs = f.pop("durations")
        f["success_rate"] = round(f["done"] / f["runs"], 4) if f["runs"] else 0.0
        f["median_s"] = round(statistics.median(durs), 3) if durs else 0.0
        f["p90_s"] = round(_pct(durs, 0.9), 3)
        f["cost_per_run"] = round(f["cost_usd"] / f["runs"], 6) if f["runs"] else 0.0
        f["cost_usd"] = round(f["cost_usd"], 6)
        pipelines.append(f)
    pipelines.sort(key=lambda f: (-f["runs"], f["pipeline"]))
    day_list = []
    for i in range(days - 1, -1, -1):
        key = _day(now - i * 86400)
        d = by_day.get(key) or {"runs": 0, "done": 0, "failed": 0, "cost_usd": 0.0,
                                "durations": [], "up": 0, "down": 0}  # fmt: skip
        durs = d.pop("durations")
        day_list.append(
            {**d, "day": key, "cost_usd": round(d["cost_usd"], 6),
             "median_s": round(statistics.median(durs), 3) if durs else 0.0}
        )  # fmt: skip
    nodes = []
    for n in per_node.values():
        durs = n.pop("durations")
        n["failure_rate"] = round(n["failures"] / n["executions"], 4) if n["executions"] else 0.0
        n["median_s"] = round(statistics.median(durs), 3) if durs else 0.0
        n["cost_usd"] = round(n["cost_usd"], 6)
        nodes.append(n)
    nodes.sort(key=lambda n: (-n["down"], -n["failure_rate"], -n["cost_usd"], n["node"]))
    out: dict[str, Any] = {
        "days": days,
        "since": since,
        "pipeline": pipeline,
        "pipelines": pipelines,
        "by_day": day_list,
        "nodes": nodes,
        "adoptions": [],
    }
    if pipeline:
        with contextlib.suppress(Exception):
            from agentline.review import ReviewStore

            out["adoptions"] = [
                {"at": a.get("at"), "node": a.get("node"), "verdict": a.get("verdict"),
                 "summary": a.get("summary")}
                for a in ReviewStore().adoptions.list()
                if a.get("flow") == pipeline and float(a.get("at") or 0) >= since
            ]  # fmt: skip
    return out
