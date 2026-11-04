"""Spend over time (from the ledger's cost records) and a monthly budget with alerts.

`report(app, days)` sums the costs of the runs created in the window by day, pipeline, model
and node. The budget lives in `$AGENTLINE_HOME/budget.json` (`{"monthly_usd", "alerted":
{"2026-10": [50, 80]}}`); the chat link sends one alert per threshold per month.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

THRESHOLDS = (50, 80, 100)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def month_key(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), UTC).strftime("%Y-%m")


def report(app: Any, days: int = 30, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    since = now - days * 86400
    month_start = (
        datetime.fromtimestamp(now, UTC)
        .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    start = min(since, month_start)
    by_day: dict[str, float] = {}
    by_flow: dict[str, dict[str, float]] = {}
    by_model: dict[str, float] = {}
    by_node: dict[str, float] = {}
    total = month = 0.0
    with app.ledger.tx() as tx:
        runs = [r for r in tx.list_runs(None, 100_000) if r.created_at >= start]
        for r in runs:
            costs = tx.list_costs(r.id)
            if not costs:
                continue
            execs = {x.id: x for x in tx.list_executions(r.id)}
            for c in costs:
                at = float(c.get("at") or r.created_at)
                usd = float(c.get("usd") or 0)
                if at >= month_start:
                    month += usd
                if at < since:
                    continue
                total += usd
                by_day[_day(at)] = by_day.get(_day(at), 0.0) + usd
                f = by_flow.setdefault(r.flow, {"usd": 0.0, "runs": 0.0})
                f["usd"] += usd
                x = execs.get(c.get("execution_id"))
                model = ((x.detail or {}).get("model") or {}).get("name") if x else None
                if not model:
                    model = str(c.get("note") or "other").replace("runner:", "")
                by_model[model] = by_model.get(model, 0.0) + usd
                if x:
                    key = f"{r.flow} / {x.node_id}"
                    by_node[key] = by_node.get(key, 0.0) + usd
        for r in runs:
            if r.created_at >= since and r.flow in by_flow:
                by_flow[r.flow]["runs"] += 1
    days_list = [_day(now - d * 86400) for d in range(days - 1, -1, -1)]
    budget = read_budget()
    return {
        "days": days,
        "total_usd": round(total, 6),
        "month_usd": round(month, 6),
        "month": month_key(now),
        "budget_usd": budget.get("monthly_usd"),
        "by_day": [{"day": d, "usd": round(by_day.get(d, 0.0), 6)} for d in days_list],
        "by_pipeline": sorted(
            (
                {"pipeline": k, "usd": round(v["usd"], 6), "runs": int(v["runs"])}
                for k, v in by_flow.items()
            ),
            key=lambda x: -x["usd"],
        ),
        "by_model": sorted(
            ({"model": k, "usd": round(v, 6)} for k, v in by_model.items()), key=lambda x: -x["usd"]
        ),
        "top_nodes": sorted(
            ({"node": k, "usd": round(v, 6)} for k, v in by_node.items()), key=lambda x: -x["usd"]
        )[:10],
    }


# -- budget ------------------------------------------------------------------------------------
def budget_path() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "budget.json"


def read_budget() -> dict[str, Any]:
    try:
        data = json.loads(budget_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_budget(data: dict[str, Any]) -> None:
    p = budget_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(".budget.json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def set_budget(monthly_usd: float | None) -> dict[str, Any]:
    data = read_budget()
    if monthly_usd is None or monthly_usd <= 0:
        data.pop("monthly_usd", None)
    else:
        data["monthly_usd"] = float(monthly_usd)
    write_budget(data)
    return data


def due_alerts(app: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Thresholds crossed this month that weren't announced yet; marks them announced."""
    data = read_budget()
    limit = data.get("monthly_usd")
    if not limit:
        return []
    rep = report(app, days=1, now=now)
    spent = rep["month_usd"]
    pct = 100.0 * spent / float(limit)
    month = rep["month"]
    alerted = set((data.get("alerted") or {}).get(month, []))
    due = [t for t in THRESHOLDS if pct >= t and t not in alerted]
    if due:
        data.setdefault("alerted", {})[month] = sorted(alerted | set(due))
        write_budget(data)
    return [{"threshold": t, "spent": spent, "budget": float(limit), "month": month} for t in due]
