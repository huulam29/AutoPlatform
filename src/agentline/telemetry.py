"""Optional OpenTelemetry export of finished runs (phase 58). Off by default; local first.

`[telemetry]` in agentline.toml:

    enabled = false
    endpoint = "http://localhost:4318"    # an OTLP/HTTP collector; /v1/traces, /v1/metrics
    service_name = "agentline"
    headers_secret = "OTEL_HEADERS"       # optional: a secret whose value is "k=v,k2=v2"

Each finished run (done / dead / cancelled) is sent once as one trace (OTLP JSON: a span for the
run, one per execution) and a few delta-sum metrics. Only ids, names, statuses, counts, costs,
tokens, models and times are sent: never prompts, inputs, outputs, comments, transcripts or error
text, and every attribute value passes the redactor. Header values come from the secrets file or
the environment and are never logged or returned. `AGENTLINE_TELEMETRY=off` stops all sending.

Progress: `<home>/telemetry/state.json` `{"since": ts, "sent": {run_id: ts}, "last": {...}}`.
A failed send keeps the runs queued (retried with backoff); queued runs older than MAX_AGE are
dropped with a warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError

log = logging.getLogger("agentline.telemetry")

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "endpoint": "",
    "service_name": "agentline",
    "headers_secret": "",
}
FINISHED = ("done", "dead", "cancelled")
BATCH = 20
INTERVAL = 15.0
MAX_BACKOFF = 300.0
MAX_AGE = 24 * 3600.0
TIMEOUT = 10.0
_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def killed() -> bool:
    return os.environ.get("AGENTLINE_TELEMETRY", "").strip().lower() in ("off", "0", "false")


def config(cfg: Any) -> dict[str, Any]:
    raw = dict(((getattr(cfg, "raw", None) or {}).get("telemetry")) or {})
    out = dict(DEFAULTS)
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    for k in ("endpoint", "service_name", "headers_secret"):
        if isinstance(raw.get(k), str):
            out[k] = raw[k].strip()
    out["service_name"] = out["service_name"] or "agentline"
    return out


def check(values: dict[str, Any]) -> dict[str, Any]:
    ep = str(values.get("endpoint") or "")
    if ep and not re.match(r"^https?://[^\s/]+", ep):
        raise ValidationError("the telemetry endpoint must be an http:// or https:// address")
    if values.get("enabled") and not ep:
        raise ValidationError("set the collector's address before turning telemetry on")
    hs = str(values.get("headers_secret") or "")
    if hs and not _NAME.match(hs):
        raise ValidationError(f"{hs!r} is not a secret name (A-Z, 0-9, _)")
    sn = str(values.get("service_name") or "")
    if len(sn) > 100 or not re.match(r"^[A-Za-z0-9_.\-]*$", sn):
        raise ValidationError("service_name: letters, digits, '.', '_' and '-' only")
    return values


def save(path: Path, changes: dict[str, Any]) -> dict[str, Any]:
    """Write `[telemetry]` (only the keys given change). Returns the new config."""
    from agentline.config import Config
    from agentline.tomlwrite import set_plain_table, write_file

    text = path.read_text() if path.exists() else ""
    current = config(Config.from_dict(tomllib.loads(text), path) if text.strip() else None)
    for k in DEFAULTS:
        if k in changes and changes[k] is not None:
            current[k] = changes[k] if k == "enabled" else str(changes[k]).strip()
    check(current)
    table = {
        "enabled": bool(current["enabled"]),
        "endpoint": current["endpoint"] or None,
        "service_name": None if current["service_name"] == "agentline" else current["service_name"],
        "headers_secret": current["headers_secret"] or None,
    }
    new = set_plain_table(text, "telemetry", table)
    Config.from_dict(tomllib.loads(new), path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file(path, new)
    return config(Config.from_dict(tomllib.loads(new), path))


def headers(conf: dict[str, Any]) -> dict[str, str]:
    """The collector's headers from the named secret (`k=v,k2=v2`, the OTEL format)."""
    from agentline.secrets import read_secrets

    name = conf.get("headers_secret") or ""
    if not name:
        return {}
    value = os.environ.get(name) or read_secrets().get(name) or ""
    out = {}
    for part in value.split(","):
        k, sep, v = part.partition("=")
        if sep and k.strip():
            out[k.strip()] = urllib.request.unquote(v.strip())
    return out


def headers_set(conf: dict[str, Any]) -> bool:
    from agentline.secrets import read_secrets

    name = conf.get("headers_secret") or ""
    return bool(name and (os.environ.get(name) or read_secrets().get(name)))


# -- state --------------------------------------------------------------------------------------
def state_path() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "telemetry" / "state.json"


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(data: dict[str, Any]) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".state.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)


# -- payloads -------------------------------------------------------------------------------------
def _hex(text: str, n: int) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[: n * 2]


def _ns(t: float | None) -> str:
    return str(int(float(t or 0) * 1e9))


def _attr(key: str, value: Any, redactor: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        v: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": redactor.redact(str(value))[:256]}
    return {"key": key, "value": v}


def _attrs(d: dict[str, Any], redactor: Any) -> list[dict[str, Any]]:
    return [a for k, v in d.items() if (a := _attr(k, v, redactor)) is not None]


def _resource(conf: dict[str, Any], redactor: Any) -> dict[str, Any]:
    from agentline import __version__

    return {
        "attributes": _attrs(
            {"service.name": conf["service_name"], "service.version": __version__}, redactor
        )
    }


_STATUS = {"done": 1, "skipped": 1, "dead": 2, "cancelled": 0}


def run_payloads(app: Any, run_id: str, conf: dict[str, Any]) -> tuple[dict, dict] | None:
    """(traces, metrics) OTLP JSON bodies for one finished run."""
    from agentline import metrics

    m = metrics.run_metrics(app, run_id)
    if m is None:
        return None
    red = app.redactor
    with app.ledger.tx() as tx:
        execs = tx.list_executions(run_id)
    trace_id = _hex(f"run:{run_id}", 16)
    root = _hex(f"span:{run_id}", 8)
    start, end = m["created_at"], m["ended_at"] or m["created_at"]
    tok = m["totals"]["tokens"] or {}
    spans = [
        {
            "traceId": trace_id,
            "spanId": root,
            "name": f"run {m['flow']}",
            "kind": 1,
            "startTimeUnixNano": _ns(start),
            "endTimeUnixNano": _ns(end),
            "attributes": _attrs(
                {
                    "agentline.run.id": run_id,
                    "agentline.pipeline": m["flow"],
                    "agentline.run.status": m["status"],
                    "agentline.cost_usd": float(m["cost_usd"]),
                    "agentline.retries": int(m["totals"]["retries"]),
                    "agentline.work_s": float(m["totals"]["work_s"]),
                    "agentline.waiting_s": float(m["totals"]["waiting_s"]),
                    "agentline.feedback.up": int(m["feedback"]["up"]),
                    "agentline.feedback.down": int(m["feedback"]["down"]),
                    "gen_ai.usage.input_tokens": tok.get("input"),
                    "gen_ai.usage.output_tokens": tok.get("output"),
                },
                red,
            ),
            "status": {"code": _STATUS.get(m["status"], 0)},
        }
    ]
    kinds = {n["node"]: n["kind"] for n in m["nodes"]}
    for x in execs:
        t = metrics.tokens_of(x.detail) or {}
        models = metrics.models_of(x.detail)
        spans.append(
            {
                "traceId": trace_id,
                "spanId": _hex(f"span:{x.id}", 8),
                "parentSpanId": root,
                "name": f"{x.node_id} #{x.iteration}",
                "kind": 1,
                "startTimeUnixNano": _ns(x.started_at or x.created_at),
                "endTimeUnixNano": _ns(x.finished_at or x.started_at or x.created_at),
                "attributes": _attrs(
                    {
                        "agentline.node": x.node_id,
                        "agentline.node.kind": kinds.get(x.node_id) or None,
                        "agentline.iteration": int(x.iteration),
                        "agentline.attempt": int(x.attempt),
                        "agentline.status": x.status,
                        "agentline.cost_usd": float(x.cost_usd or 0.0),
                        "gen_ai.request.model": models[0] if models else None,
                        "gen_ai.usage.input_tokens": t.get("input"),
                        "gen_ai.usage.output_tokens": t.get("output"),
                        "agentline.turns": metrics.turns_of(x.detail),
                    },
                    red,
                ),
                "status": {"code": _STATUS.get(x.status, 0)},
            }
        )
    scope = {"name": "agentline", "version": "1"}
    traces = {
        "resourceSpans": [
            {"resource": _resource(conf, red), "scopeSpans": [{"scope": scope, "spans": spans}]}
        ]
    }
    labels = {"agentline.pipeline": m["flow"], "agentline.run.status": m["status"]}

    def point(value: float | int) -> dict[str, Any]:
        p: dict[str, Any] = {
            "attributes": _attrs(labels, red),
            "startTimeUnixNano": _ns(start),
            "timeUnixNano": _ns(end),
        }
        p["asInt" if isinstance(value, int) else "asDouble"] = value
        return p

    def metric(name: str, unit: str, value: float | int) -> dict[str, Any]:
        return {
            "name": name,
            "unit": unit,
            "sum": {
                "dataPoints": [point(value)],
                "aggregationTemporality": 1,  # delta
                "isMonotonic": True,
            },
        }

    ms = [
        metric("agentline.runs", "1", 1),
        metric("agentline.cost_usd", "USD", float(m["cost_usd"])),
        metric("agentline.run.duration_s", "s", float(m["wall_s"])),
        metric("agentline.retries", "1", int(m["totals"]["retries"])),
        metric("agentline.feedback.down", "1", int(m["feedback"]["down"])),
    ]
    if tok:
        ms.append(metric("agentline.tokens", "1", int(sum(tok.values()))))
    metrics_body = {
        "resourceMetrics": [
            {"resource": _resource(conf, red), "scopeMetrics": [{"scope": scope, "metrics": ms}]}
        ]
    }
    return traces, metrics_body


def post(conf: dict[str, Any], path: str, body: dict[str, Any]) -> int:
    url = conf["endpoint"].rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", **headers(conf)},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # the user's own collector
        return int(r.status)


def _error(e: Exception, conf: dict[str, Any]) -> str:
    msg = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(e)
    for v in headers(conf).values():  # a header value must never reach a log or a reply
        if v:
            msg = msg.replace(v, "***")
    return msg[:300]


def test_send(app: Any, conf: dict[str, Any]) -> dict[str, Any]:
    """Send one `agentline.test` span. {ok, status?, error?}"""
    if not conf.get("endpoint"):
        return {"ok": False, "error": "no endpoint set"}
    if killed():
        return {"ok": False, "error": "telemetry is turned off here (AGENTLINE_TELEMETRY=off)"}
    now = time.time()
    span = {
        "traceId": _hex(f"test:{now}", 16),
        "spanId": _hex(f"test-span:{now}", 8),
        "name": "agentline.test",
        "kind": 1,
        "startTimeUnixNano": _ns(now),
        "endTimeUnixNano": _ns(now),
        "attributes": [],
    }
    body = {
        "resourceSpans": [
            {
                "resource": _resource(conf, app.redactor),
                "scopeSpans": [{"scope": {"name": "agentline"}, "spans": [span]}],
            }
        ]
    }
    try:
        return {"ok": True, "status": post(conf, "/v1/traces", body)}
    except (OSError, urllib.error.URLError, ValueError) as e:
        return {"ok": False, "error": _error(e, conf)}


def due_runs(app: Any, st: dict[str, Any], now: float) -> list[str]:
    since = float(st.get("since") or now)
    sent = st.get("sent") or {}
    with app.ledger.tx() as tx:
        runs = [r for s in FINISHED for r in tx.list_runs(s, 500)]
    due = sorted(
        (r for r in runs if r.updated_at >= since and r.id not in sent),
        key=lambda r: r.updated_at,
    )
    return [r.id for r in due]


def export_once(app: Any, conf: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Send what's due (one batch). Returns {sent, failed, error?, queued}."""
    now = time.time() if now is None else now
    st = read_state()
    if "since" not in st:  # start from when telemetry was first turned on, not the past
        st["since"] = now
        write_state(st)
        return {"sent": 0, "failed": 0, "queued": 0}
    sent: dict[str, float] = dict(st.get("sent") or {})
    due = due_runs(app, st, now)
    with app.ledger.tx() as tx:
        ended = {rid: (tx.get_run(rid) or None) for rid in due}
    out: dict[str, Any] = {"sent": 0, "failed": 0}
    for rid in due[:BATCH]:
        r = ended.get(rid)
        if r is not None and now - r.updated_at > MAX_AGE:
            log.warning("telemetry: dropped run %s (queued over 24 h)", rid)
            sent[rid] = -now
            continue
        bodies = run_payloads(app, rid, conf)
        if bodies is None:
            sent[rid] = -now
            continue
        try:
            post(conf, "/v1/traces", bodies[0])
            post(conf, "/v1/metrics", bodies[1])
        except (OSError, urllib.error.URLError, ValueError) as e:
            out["failed"] += 1
            out["error"] = _error(e, conf)
            break  # keep the order: the rest waits for the next try
        sent[rid] = now
        out["sent"] += 1
    # runs that ended over MAX_AGE ago are never due, so the memory of sent runs stays small:
    # a run sent before now - 2 * MAX_AGE ended before now - MAX_AGE
    st["since"] = max(float(st["since"]), now - MAX_AGE)
    st["sent"] = {k: v for k, v in sent.items() if abs(v) >= now - 2 * MAX_AGE}
    st["last"] = {
        "at": now,
        "ok": not out["failed"],
        "error": out.get("error"),
        "sent": out["sent"],
    }
    write_state(st)
    out["queued"] = max(0, len(due) - out["sent"])
    return out


async def run_telemetry_loop(
    app: Any, files: Any, stop: asyncio.Event, *, interval: float = INTERVAL
) -> None:
    """Export finished runs while telemetry is on (read each tick: settings apply live)."""
    backoff = interval
    while not stop.is_set():
        wait = interval
        try:
            conf = config(getattr(files, "config", None))
            if conf["enabled"] and conf["endpoint"] and not killed():
                res = await asyncio.to_thread(export_once, app, conf)
                if res.get("failed"):
                    log.warning("telemetry: send failed: %s", res.get("error"))
                    backoff = min(MAX_BACKOFF, backoff * 2)
                    wait = backoff
                else:
                    backoff = interval
        except Exception as e:  # never let telemetry take the server down
            log.warning("telemetry loop: %s", e)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), wait)


def status(cfg: Any) -> dict[str, Any]:
    conf = config(cfg)
    st = read_state()
    return {
        **conf,
        "headers_set": headers_set(conf),
        "killed": killed(),
        "last": st.get("last"),
    }
