"""Run review and harness learning (phase 38; ideas from Google Research's Harness²).

Triage (phase 28) asks "why did this fail?". A review asks "how should this pipeline's
instructions change, judging by the whole run?", for runs that succeeded the hard way too.

- **Signals** (`signals`, no model call): review loops, gate rejections, retries, needs-review,
  empty agent output, a cost / time spike against the pipeline's recent median, and the run's
  triage verdicts. With `[review] auto = "signals"` (the default) a run that ends with any of
  them is reviewed automatically; `"all"` reviews every finished run, `"off"` none. A click
  (API, Chat, CLI) reviews any run.
- **Review** (`review`): the Chat's harness reads the whole run (every execution's inputs,
  output, journal and transcript tail; the pipeline and its agent nodes' files; the pipeline's
  lesson library and earlier adopted edits) and answers with rows (node, component, what it
  observed, a verdict), edit cards and at most one lesson. Cards are checked like Chat
  proposals; nothing changes until the user applies one (decision 71).
- **Adoption and follow-up** (`apply_edit`, `follow_up`, no model call): a review's edit card
  is applied here (so the files as they were are kept for an undo) and recorded with the
  pipeline's trouble rate over its last runs. After `followup_runs` more runs that executed the
  node, the rate before vs after gives helped / hurt / no effect; the lesson's status follows,
  and *hurt* offers a card that puts the files back as they were.
- **Probes**: agentline.probe replays one node with a proposed edit.

Costs count against the run's `budget_usd` (note "review" / "probe"); a review is skipped while
it's used up. Records: `$AGENTLINE_HOME/reviews/v<12 hex>.json`, adoptions in
`reviews/adoptions/a<12 hex>.json`, `reviews/state.json` (when automatic review began).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from agentline import lessons as lib
from agentline.errors import ValidationError

log = logging.getLogger("agentline.review")

REVIEWED_RUN = ("done", "dead")
ROW_VERDICTS = ("problem", "helped", "hurt", "no effect", "confounded")
CARD_TYPES = ("pipeline", "setting", "lesson")
MAX_EVIDENCE = 40_000
MAX_OUTPUT = 1_200
MAX_TRANSCRIPT = 1_500
MAX_FILE = 8_000
TRIAGE_GRACE = 120.0  # wait this long for a dead run's triage before reviewing without it
SETTLE = 3.0
LOOP_INTERVAL = 10.0
DEFAULTS: dict[str, Any] = {
    "auto": "signals",
    "spike_factor": 2.0,
    "spike_min_runs": 5,
    "max_probes": 2,
    "probe_budget_usd": 1.0,
    "followup_runs": 5,
}


def reviews_dir() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "reviews"


def config(cfg: Any) -> dict[str, Any]:
    """`[review]` in agentline.toml, with the defaults filled in (bad values fall back)."""
    raw = dict(((getattr(cfg, "raw", None) or {}).get("review")) or {})
    out = dict(DEFAULTS)
    if raw.get("auto") in ("signals", "all", "off"):
        out["auto"] = raw["auto"]
    for k in ("spike_factor", "probe_budget_usd"):
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[k] = float(v)
    for k in ("spike_min_runs", "max_probes", "followup_runs"):
        v = raw.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            out[k] = v
    return out


def review_id(run_id: str) -> str:
    return "v" + hashlib.sha1(f"review:{run_id}".encode()).hexdigest()[:12]


# -- records ----------------------------------------------------------------------------------
class _JsonStore:
    prefix = "v"
    glob = "v*.json"

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else reviews_dir()

    def _path(self, rid: str) -> Path:
        if not re.fullmatch(rf"{self.prefix}[0-9a-f]{{12}}", rid):
            raise KeyError(rid)
        return self.root / f"{rid}.json"

    def get(self, rid: str) -> dict[str, Any] | None:
        try:
            return dict(json.loads(self._path(rid).read_text(encoding="utf-8")))
        except (KeyError, OSError, ValueError):
            return None

    def save(self, rec: dict[str, Any]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        rec["updated_at"] = time.time()
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".rec.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, default=str)
        os.replace(tmp, self._path(str(rec["id"])))
        return rec

    def all(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        out = []
        for p in self.root.glob(self.glob):
            with contextlib.suppress(OSError, ValueError):
                rec = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(rec, dict):
                    out.append(rec)
        out.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return out


class ReviewStore(_JsonStore):
    def list(
        self, run: str | None = None, flow: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Newest first."""
        out = [
            r
            for r in self.all()
            if (run is None or r.get("run") == run) and (flow is None or r.get("flow") == flow)
        ]
        return out[:limit]

    def since(self) -> float:
        """When automatic review began in this data folder (set on first use)."""
        p = self.root / "state.json"
        with contextlib.suppress(OSError, ValueError):
            return float(json.loads(p.read_text(encoding="utf-8"))["since"])
        self.root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        p.write_text(json.dumps({"since": now}), encoding="utf-8")
        return now

    @property
    def adoptions(self) -> AdoptionStore:
        return AdoptionStore(self.root / "adoptions")


class AdoptionStore(_JsonStore):
    prefix = "a"
    glob = "a*.json"

    def list(self, flow: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.all() if flow is None or r.get("flow") == flow]


# -- signals ----------------------------------------------------------------------------------
def _node_type(graph: Any, node: str) -> str:
    with contextlib.suppress(Exception):
        return str(graph.nodes[node].meta.get("type") or graph.nodes[node].kind)
    return ""


def _trouble(sig: dict[str, Any]) -> float:
    """One number per run for before / after comparisons: how much went the hard way."""
    return float(
        sum(x["count"] for x in sig["loops"])
        + sum(x["attempts"] - 1 for x in sig["retries"])
        + sig["manual_retries"]
        + len(sig["gate_rejections"])
        + len(sig["needs_review"])
        + len(sig["empty"])
    )


def signals(
    app: Any, run_id: str, *, cfg: Any = None, triage_store: Any = None, spike: bool = True
) -> dict[str, Any] | None:
    """What a run did the hard way (no model call). None when there's no such run."""
    from agentline.triage import TriageStore, _empty

    with app.ledger.tx() as tx:
        run = tx.get_run(run_id)
        if run is None:
            return None
        execs = tx.list_executions(run_id)
        journal = tx.list_journal(run_id)
        graph = None
        with contextlib.suppress(Exception):
            graph = app.graph(run.flow, run)
        loops = []
        if graph is not None:
            for e in graph.edges:
                if e.loop and (n := tx.loop_count(run_id, e.id)):
                    loops.append({"edge": e.id, "count": n})
        else:  # the pipeline isn't loaded: a node's extra iterations stand in for its loops
            top: dict[str, int] = {}
            for x in execs:
                top[x.node_id] = max(top.get(x.node_id, 1), x.iteration)
            loops = [{"edge": f"{n} (iterations)", "count": i - 1} for n, i in top.items() if i > 1]
        empty = []
        for x in execs:
            if x.status == "done" and x.result_ref and _node_type(graph, x.node_id) == "agent":
                with contextlib.suppress(Exception):
                    if _empty(app.scheduler.artifacts.load(tx, x.result_ref)):
                        empty.append(x.node_id)
        recent = [r for r in tx.list_runs(None, 400) if r.flow == run.flow and r.id != run.id]
    retries = [
        {"node": x.node_id, "iteration": x.iteration, "attempts": x.attempt}
        for x in execs
        if x.attempt > 1
    ]
    rejections = [
        {"node": x.node_id, "iteration": x.iteration, "comment": str(x.error or "")[:500]}
        for x in execs
        if x.status == "dead" and _node_type(graph, x.node_id) == "gate"
    ]
    needs_review = sorted(
        {j.node_id for j in journal if j.node_id and j.message.endswith("-> needs-review")}
    )
    duration = max(0.0, float(run.updated_at) - float(run.created_at))
    out: dict[str, Any] = {
        "run": run.id,
        "flow": run.flow,
        "status": run.status,
        "cost_usd": float(run.cost_usd or 0.0),
        "duration_s": duration,
        "loops": loops,
        "retries": retries,
        "manual_retries": sum(1 for j in journal if j.kind == "retry"),
        "gate_rejections": rejections,
        "needs_review": needs_review,
        "empty": sorted(set(empty)),
        "spike": None,
        "triage": [],
        "nodes": sorted({x.node_id for x in execs}),
    }
    if spike:
        rc = config(cfg)
        done = [r for r in recent if r.status in REVIEWED_RUN][:20]
        if len(done) >= max(1, rc["spike_min_runs"]):
            mc = statistics.median(float(r.cost_usd or 0.0) for r in done)
            md = statistics.median(max(0.0, r.updated_at - r.created_at) for r in done)
            f = rc["spike_factor"]
            hit = {}
            if mc > 0 and out["cost_usd"] > f * mc:
                hit["cost"] = {"run": out["cost_usd"], "median": mc}
            if md > 0 and duration > f * md:
                hit["duration"] = {"run": duration, "median": md}
            out["spike"] = hit or None
    store = triage_store if triage_store is not None else TriageStore()
    out["triage"] = [
        {k: t.get(k) for k in ("id", "node", "verdict", "status", "summary")}
        for t in store.list(run_id)
    ]
    out["trouble"] = _trouble(out)
    return out


def has_signals(sig: dict[str, Any]) -> bool:
    return bool(sig["trouble"] or sig["spike"] or sig["triage"])


def signal_lines(sig: dict[str, Any]) -> list[str]:
    """The signals in words (the prompt, the CLI, chat links)."""
    out = []
    for x in sig["loops"]:
        out.append(f"loop {x['edge']} taken {x['count']} time(s)")
    for x in sig["gate_rejections"]:
        out.append(f"gate {x['node']} rejected" + (f": {x['comment']}" if x["comment"] else ""))
    for x in sig["retries"]:
        out.append(f"{x['node']} (iteration {x['iteration']}) took {x['attempts']} attempts")
    if sig["manual_retries"]:
        out.append(f"retried by hand {sig['manual_retries']} time(s)")
    for n in sig["needs_review"]:
        out.append(f"{n} stopped for review (needs-review)")
    for n in sig["empty"]:
        out.append(f"agent node {n} finished with an empty result")
    for k, v in (sig["spike"] or {}).items():
        unit = "$" if k == "cost" else ""
        fmt = (lambda x: f"${x:.2f}") if unit else (lambda x: f"{x:.0f} s")
        out.append(f"{k} spike: {fmt(v['run'])} vs a median of {fmt(v['median'])}")
    for t in sig["triage"]:
        out.append(f"triage of {t.get('node') or 'the run'}: {t.get('verdict') or t.get('status')}")
    return out


# -- evidence and the prompt ------------------------------------------------------------------
GUIDE = """\
You are agentline's run reviewer. agentline runs multi-agent pipelines described in YAML
(agent / action / gate / router / wait nodes); each agent node has a prompt, an optional
output_schema, skills, MCP servers, a harness and a model. A run of this pipeline has ended.
Read the WHOLE run in the EVIDENCE and decide how the pipeline's instructions should change so
later runs go better: fewer review loops, gate rejections, retries and stops for review, less
cost. A run that succeeded after loops still has something to teach.

Write three things.

1. ROWS: one per thing you observed that the pipeline's instructions caused or could prevent.
   Each row names the node and the component (prompt, output_schema, skills, mcp, model,
   harness, allowed_tools, max_turns, max_loops, router, pipeline), says what you observed and
   where in the evidence (execution, iteration, journal or transcript line), and gives a
   verdict: "problem" (it went wrong because of this), or, for an edit adopted earlier (see
   EARLIER ADOPTED EDITS), "helped", "hurt", "no effect" or "confounded". PREFER "confounded"
   when several things changed at once or one run is all there is: a made-up attribution
   poisons every later review.

2. ACTIONS: edit cards that fix the problems, shown to the user to apply:
   {"type": "pipeline", "path": "<PIPELINE FILE>", "node": "<the node it changes>",
    "confidence": "high" | "medium" | "low", "summary": "...", "yaml": "<the WHOLE new YAML>",
    "files": {"prompts/x.md": "<whole new file>"}}
   Change only what the evidence argues for and keep everything else as it is. One pipeline
   action per problem (each is tried on its own). A rule for building pipelines in general
   (not this one) may be a {"type": "lesson", "text": "<one line>"} for the Chat instructions.
   No change at all is a real answer when the run shows nothing the instructions could fix:
   set "keep": true.

3. LESSON: at most ONE entry for this pipeline's lesson library, and only if the rows support
   it. "cue" is the SHAPE of situation it applies to (so it fires on a different ticket), not
   this run's facts; "lesson" is the change to make, naming the component; "evidence" cites the
   rows; "confidence" high / medium / low with why. Not too generic ("be thorough") and not too
   specific ("ticket 412 needs a CHANGELOG line"); aim between ("when a reviewer checks a repo
   convention the writer is never told about, state the convention in the writer's prompt").
   Label facts about this repository ("in this repo, ...") apart from procedure. Lessons
   already in the LESSON LIBRARY are hypotheses from earlier runs, not facts: re-check them
   against this run and don't repeat one. If the record shows no reusable lesson, "lesson":
   null.

Failed nodes are triaged separately (TRIAGE OF THIS RUN): don't propose the same fix again.

Reply with ONE JSON object and nothing else:
{
  "summary": "one line: how the run went and the main thing to change",
  "keep": false,
  "rows": [{"node": "...", "component": "prompt", "observed": "...", "evidence": "...",
            "verdict": "problem"}],
  "actions": [ ... ],
  "lesson": {"node": "...", "component": "prompt", "cue": "...", "lesson": "...",
             "evidence": "...", "confidence": "medium"} | null
}
"""


def _fmt_s(s: float) -> str:
    return f"{s / 60:.1f} min" if s >= 90 else f"{s:.0f} s"


def _value_text(app: Any, tx: Any, digest: str | None, limit: int = MAX_OUTPUT) -> str:
    if not digest:
        return "(none)"
    with contextlib.suppress(Exception):
        meta = app.scheduler.artifacts.meta(tx, digest)
        value = app.scheduler.artifacts.load(tx, digest)
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        head = f"[{meta.summary}] " if meta is not None and meta.summary else ""
        return head + (text[:limit] + (" …" if len(text) > limit else ""))
    return "(unreadable)"


def _transcript(app: Any, tx: Any, x: Any) -> str:
    atts = (x.detail or {}).get("attachments") or {}
    names = sorted(n for n in atts if n.startswith("transcript"))
    if not names:
        return ""
    digest = atts[names[-1]].get("ref")
    with contextlib.suppress(Exception):
        data = app.scheduler.artifacts.read_bytes(tx, digest) or b""
        return data.decode("utf-8", errors="replace")[-MAX_TRANSCRIPT:]
    return ""


def run_text(app: Any, run_id: str, sig: dict[str, Any]) -> str:
    """The whole run, execution by execution, for the reviewer (not yet redacted)."""
    with app.ledger.tx() as tx:
        run = tx.get_run(run_id)
        if run is None:
            return ""
        execs = sorted(tx.list_executions(run_id), key=lambda e: (e.created_at, e.iteration))
        journal = tx.list_journal(run_id)
        links = tx.list_artifact_links(run_id)
        graph = None
        with contextlib.suppress(Exception):
            graph = app.graph(run.flow, run)
        budget = f" of a ${run.budget_usd:.2f} budget" if run.budget_usd is not None else ""
        lines = [
            f"Run {run.id} of pipeline {run.flow} ended {run.status} after"
            f" {_fmt_s(sig['duration_s'])}, cost ${sig['cost_usd']:.2f}{budget}.",
            f"Run error: {run.error}" if run.error else "",
            "",
            "## SIGNALS",
            *(f"- {s}" for s in signal_lines(sig)),
            "" if signal_lines(sig) else "- none",
            "",
            "## EXECUTIONS (in order)",
        ]
        for x in execs:
            kind = _node_type(graph, x.node_id) or "?"
            took = (x.finished_at or 0) - (x.started_at or 0) if x.started_at else 0
            lines.append(
                f"\n### {x.node_id} #{x.iteration} ({kind}, {x.status}, attempt {x.attempt},"
                f" ${x.cost_usd:.2f}, {_fmt_s(max(0.0, took))})"
            )
            reads = [ln for ln in links if ln.execution_id == x.id and ln.direction == "read"]
            if reads:
                lines.append(
                    "inputs: "
                    + "; ".join(f"{ln.name} = {_value_text(app, tx, ln.hash, 300)}" for ln in reads)
                )
            if x.error:
                lines.append(f"error: {x.error[:1500]}")
            if x.reason:
                lines.append(f"reason: {x.reason}")
            if x.status == "done":
                lines.append(f"output: {_value_text(app, tx, x.result_ref)}")
            notes = [
                j
                for j in journal
                if j.execution_id == x.id and (j.kind != "status" or j.detail.get("error"))
            ]
            for j in notes[-8:]:
                err = j.detail.get("error")
                lines.append(f"journal: {j.message}" + (f" ({str(err)[:300]})" if err else ""))
            if kind == "agent" and (t := _transcript(app, tx, x)):
                lines.append(f"transcript (last {MAX_TRANSCRIPT} chars):\n{t}")
    return "\n".join(ln for ln in lines if ln is not None)


def _agent_files(path: Path, yaml_text: str) -> list[tuple[str, str]]:
    """Every agent node's prompt / output_schema file (when they are files)."""
    from ruamel.yaml import YAML

    try:
        nodes = (YAML(typ="safe").load(yaml_text) or {}).get("nodes") or {}
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for n in nodes.values():
        if not isinstance(n, dict) or n.get("type") != "agent":
            continue
        for fld in ("prompt", "output_schema"):
            v = n.get(fld)
            if not isinstance(v, str) or "\n" in v or len(v) > 300 or v in seen:
                continue
            p = (path.parent / v).resolve()
            if p.is_file() and p.is_relative_to(path.parent.resolve()):
                with contextlib.suppress(OSError):
                    out.append((v, p.read_text(encoding="utf-8")[:MAX_FILE]))
                    seen.add(v)
    return out


def pipeline_file(files: Any, flow: str) -> Path | None:
    from agentline.triage import _pipeline_file

    hit = _pipeline_file(files, flow)
    return hit[0] if hit else None


def adoptions_text(adoptions: list[dict[str, Any]]) -> str:
    lines = []
    for a in adoptions[:10]:
        v = a.get("verdict") or f"waiting ({a.get('after', {}).get('runs', 0)} runs so far)"
        lines.append(
            f"- {time.strftime('%Y-%m-%d', time.localtime(a.get('at') or 0))}: {a.get('node')}"
            f" {a.get('component') or ''}: {a.get('summary') or ''} -> {v}"
            + (f" ({a['detail']})" if a.get("detail") else "")
        )
    return "\n".join(lines) or "none"


def evidence(
    app: Any, files: Any, run_id: str, sig: dict[str, Any], store: ReviewStore
) -> dict[str, Any]:
    """{text, pipeline (editor name), path (relative), yaml, files, lessons, file (Path)}."""
    text = app.redactor.redact(run_text(app, run_id, sig))
    out: dict[str, Any] = {
        "text": text[-MAX_EVIDENCE:],
        "pipeline": None,
        "path": None,
        "yaml": None,
        "files": [],
        "lessons": "",
        "file": None,
    }
    from agentline.triage import _pipeline_file

    hit = _pipeline_file(files, sig["flow"])
    if hit:
        path, yaml_text = hit
        out["pipeline"] = files.name_of(path)
        out["path"] = out["pipeline"] + ".yaml"
        out["yaml"] = yaml_text
        out["files"] = _agent_files(path, yaml_text)
        out["lessons"] = lib.text_for_prompt(path)
        out["file"] = path
    out["adoptions"] = adoptions_text(store.adoptions.list(sig["flow"]))
    return out


def build_prompt(sig: dict[str, Any], ev: dict[str, Any], triage_text: str) -> str:
    parts = [GUIDE, "## EVIDENCE\n" + ev["text"]]
    parts.append("## TRIAGE OF THIS RUN\n" + (triage_text or "none"))
    if ev.get("yaml") is not None:
        parts.append(f"## PIPELINE FILE {ev['path']}\n{ev['yaml']}")
        for rel, text in ev.get("files") or []:
            parts.append(f"## FILE {rel} (relative to the pipeline's folder)\n{text}")
    else:
        parts.append("## PIPELINE FILE\n(not found in the pipelines folder: no pipeline action)")
    parts.append("## LESSON LIBRARY of this pipeline\n" + (ev.get("lessons") or "empty"))
    parts.append("## EARLIER ADOPTED EDITS of this pipeline\n" + ev.get("adoptions", "none"))
    return "\n\n".join(parts) + "\n\nReply with the JSON object."


def _one(v: Any, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def parse_reply(output: Any) -> dict[str, Any]:
    from agentline.assistant import _json_object

    data = output if isinstance(output, dict) else _json_object(str(output or ""))
    if not data:
        raise ValidationError("the reviewer didn't answer with a JSON object")
    rows = []
    for r in data.get("rows") or []:
        if not isinstance(r, dict) or not r.get("node"):
            continue
        verdict = str(r.get("verdict") or "problem").strip().lower()
        comp = str(r.get("component") or "prompt").strip()
        rows.append(
            {
                "node": _one(r["node"], 100),
                "component": comp if comp in lib.COMPONENTS else "pipeline",
                "observed": str(r.get("observed") or "").strip()[:1500],
                "evidence": str(r.get("evidence") or "").strip()[:800],
                "verdict": verdict if verdict in ROW_VERDICTS else "confounded",
            }
        )
    lesson = data.get("lesson")
    if not isinstance(lesson, dict) or not lesson.get("lesson") or not lesson.get("cue"):
        lesson = None
    else:
        lesson = {
            k: _one(lesson.get(k), lib.MAX_TEXT)
            for k in ("node", "component", "cue", "lesson", "evidence", "confidence")
        }
    actions = [
        a
        for a in (data.get("actions") or [])
        if isinstance(a, dict) and a.get("type") in CARD_TYPES
    ]
    return {
        "summary": _one(data.get("summary"), 300),
        "keep": bool(data.get("keep")) and not actions,
        "rows": rows[:40],
        "actions": actions[:8],
        "lesson": lesson,
    }


# -- one review -------------------------------------------------------------------------------
_inflight: set[str] = set()


def _record(run_id: str, flow: str, manual: bool) -> dict[str, Any]:
    now = time.time()
    return {
        "id": review_id(run_id),
        "run": run_id,
        "flow": flow,
        "manual": manual,
        "status": "running",
        "signals": None,
        "summary": "",
        "keep": False,
        "rows": [],
        "lesson": None,
        "actions": [],
        "probes": {},
        "adopted": {},
        "lesson_added": None,  # the lesson card's entry in lessons.md, once applied
        "pipeline": None,
        "harness": None,
        "model": None,
        "cost_usd": 0.0,
        "probe_cost_usd": 0.0,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }


def charge(app: Any, run_id: str, usd: float, note: str) -> None:
    """A review or probe call is a cost of the run (and counts against its budget); recorded
    without touching the run's updated_at (issues.sync would see new activity) and kept out of
    the step's own cost (a cost row needs an execution: the run's last one)."""
    if usd <= 0:
        return
    with app.ledger.tx() as tx:
        execs = tx.list_executions(run_id)
        if not execs:
            return
        last = max(execs, key=lambda e: e.finished_at or e.started_at or 0)
        tx.add_cost(run_id, last.id, last.attempt, usd, note, touch=False, on_execution=False)


def _cards(rec: dict[str, Any], got: dict[str, Any], ev: dict[str, Any]) -> list[dict[str, Any]]:
    actions = []
    for a in got["actions"]:
        a = dict(a)
        a["review"] = rec["id"]
        if a.get("type") == "pipeline":
            conf = str(a.get("confidence") or "low").lower()
            a["confidence"] = conf if conf in lib.CONFIDENCE else "low"
        actions.append(a)
    lesson = got["lesson"]
    if lesson and ev.get("pipeline"):
        actions.append(
            {
                "type": "pipeline_lesson",
                "pipeline": ev["pipeline"],
                **lesson,
                "review": rec["id"],
                "runs": rec["run"],
                "why": "for later reviews of this pipeline",
            }
        )
    return actions


async def review(
    app: Any,
    files: Any,
    run_id: str,
    *,
    store: ReviewStore | None = None,
    triage_store: Any = None,
    manual: bool = False,
    force: bool = False,
    live: Any = None,
    sig: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Review one run and save the record (also when it fails or is skipped). `force` redoes
    one that exists. Probes are separate (agentline.probe)."""
    from agentline import assistant
    from agentline.triage import _budget_left, _chat_harness

    store = store or ReviewStore()
    cfg = getattr(files, "config", None)
    rid = review_id(run_id)
    old = store.get(rid)
    if old is not None and not force and old.get("status") != "failed":
        return old
    if rid in _inflight:
        return old or {**_record(run_id, "", manual), "status": "running"}
    _inflight.add(rid)
    try:
        sig = sig or await asyncio.to_thread(
            signals, app, run_id, cfg=cfg, triage_store=triage_store
        )
        if sig is None:
            raise KeyError(run_id)
        rec = _record(run_id, sig["flow"], manual)
        rec["signals"] = sig
        if old is not None:
            rec["created_at"] = old.get("created_at", rec["created_at"])
            rec["adopted"] = old.get("adopted") or {}
            rec["lesson_added"] = old.get("lesson_added")
        ok, why = await asyncio.to_thread(_budget_left, app, run_id)
        if not ok:
            rec.update(status="skipped", error=why.replace("triage", "a review"))
            return store.save(rec)
        store.save(rec)
        harness, model = _chat_harness()
        rec.update(harness=harness, model=model)
        ev = await asyncio.to_thread(evidence, app, files, run_id, sig, store)
        rec["pipeline"] = ev["pipeline"]
        triage_text = "\n".join(
            f"- {t.get('node') or 'the run'}: {t.get('verdict') or t.get('status')}:"
            f" {t.get('summary') or ''}"
            for t in sig["triage"]
        )
        prompt = build_prompt(sig, ev, triage_text)
        try:
            output, cost = await assistant._ask(files, harness, model, prompt)
        except assistant.AssistantError as e:
            rec.update(status="failed", error=str(e))
            return store.save(rec)
        rec["cost_usd"] = float(cost or 0.0)
        await asyncio.to_thread(charge, app, run_id, rec["cost_usd"], "review")
        try:
            got = app.redactor.redact_value(parse_reply(output))  # nothing secret is kept
        except ValidationError as e:
            rec.update(status="failed", error=str(e))
            return store.save(rec)
        rec.update({k: got[k] for k in ("summary", "keep", "rows", "lesson")})
        actions = _cards(rec, got, ev)
        if files is not None:
            if live is None:
                live = assistant.gather_live(files, app)
            actions = assistant.check_actions(
                actions, files.root, files.registry, files.config, live
            )
        else:
            actions = [{**a, "issues": []} for a in actions]
        rec["actions"] = actions
        rec["status"] = "done"
        return store.save(rec)
    finally:
        _inflight.discard(rid)


def probe_candidates(rec: dict[str, Any], cfg: Any) -> list[int]:
    """The proposals probed automatically: valid pipeline edits of medium or high confidence,
    at most `max_probes`, not probed yet."""
    rc = config(cfg)
    out = []
    for i, a in enumerate(rec.get("actions") or []):
        if (
            a.get("type") == "pipeline"
            and not a.get("issues")
            and a.get("confidence") in ("high", "medium")
            and str(i) not in (rec.get("probes") or {})
        ):
            out.append(i)
    return out[: rc["max_probes"]]


# -- adoption and follow-up -------------------------------------------------------------------
def _ran_node(app: Any, run_id: str, node: str) -> bool:
    with app.ledger.tx() as tx:
        return any(x.node_id == node for x in tx.list_executions(run_id))


def _rates(app: Any, runs: list[Any], node: str | None) -> dict[str, Any]:
    picked = []
    trouble = []
    cost = []
    for r in runs:
        if node and not _ran_node(app, r.id, node):
            continue
        s = signals(app, r.id, spike=False, triage_store=_NoTriage())
        if s is None:
            continue
        picked.append(r.id)
        trouble.append(s["trouble"])
        cost.append(s["cost_usd"])
    n = len(picked)
    return {
        "runs": n,
        "run_ids": picked,
        "trouble": (sum(trouble) / n) if n else 0.0,
        "cost_usd": (sum(cost) / n) if n else 0.0,
    }


class _NoTriage:
    def list(self, run: Any = None) -> list[Any]:
        return []


def _finished_runs(app: Any, flow: str, *, before: float | None = None,
                   after: float | None = None) -> list[Any]:  # fmt: skip
    with app.ledger.tx() as tx:
        runs = [r for r in tx.list_runs(None, 1000) if r.flow == flow and r.status in REVIEWED_RUN]
    if before is not None:
        runs = [r for r in runs if r.created_at < before]  # newest first
    if after is not None:
        runs = sorted((r for r in runs if r.created_at >= after), key=lambda r: r.created_at)
    return runs


def _snapshot(files: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The edit's files as they are now: {yaml, files: {rel: text | None (didn't exist)}}."""
    target = (files.root / str(a["path"])).resolve()
    out: dict[str, Any] = {"yaml": None, "files": {}}
    with contextlib.suppress(OSError):
        out["yaml"] = target.read_text(encoding="utf-8")
    for rel in a.get("files") or {}:
        try:
            out["files"][rel] = (target.parent / rel).read_text(encoding="utf-8")
        except OSError:
            out["files"][rel] = None
    return out


def apply_edit(
    app: Any,
    files: Any,
    rec: dict[str, Any],
    index: int,
    store: ReviewStore,
) -> dict[str, Any]:
    """Apply proposal `index` of review `rec` (a pipeline edit) and remember it for
    `follow_up`: the files as they were (for an undo), the history commit, and the pipeline's
    trouble rate over its last runs. Applying it again changes and records nothing."""
    from agentline import assistant, history

    actions = rec.get("actions") or []
    if not 0 <= index < len(actions) or actions[index].get("type") != "pipeline":
        raise ValidationError(f"proposal {index} of review {rec['id']} isn't a pipeline edit")
    adopted = rec.setdefault("adopted", {})
    if str(index) in adopted and (got := store.adoptions.get(adopted[str(index)])) is not None:
        return got
    a = actions[index]
    if a.get("issues"):
        raise ValidationError("the proposal doesn't validate: " + "; ".join(a["issues"]))
    with files.lock:
        before = _snapshot(files, a)
        history.record(files.root, "before review edit")
        assistant.write_pipeline(files.root, a["path"], a["yaml"], dict(a.get("files") or {}))
        commit = history.record(files.root, f"review {rec['id']}: {a['path']}")
    return adopt(app, files, rec, index, store, before=before, commit=commit)


def adopt(
    app: Any,
    files: Any,
    rec: dict[str, Any],
    index: int,
    store: ReviewStore,
    *,
    before: dict[str, Any] | None = None,
    commit: str | None = None,
) -> dict[str, Any]:
    """Record proposal `index` of review `rec` as adopted, with the pipeline's trouble rate
    before (its last `followup_runs` runs that executed the node, else the reviewed run)."""
    a = (rec.get("actions") or [])[index]
    cfg = getattr(files, "config", None)
    rc = config(cfg)
    now = time.time()
    aid = "a" + hashlib.sha1(f"{rec['id']}:{index}:{now}".encode()).hexdigest()[:12]
    node = str(a.get("node") or "") or None
    prior = _finished_runs(app, rec["flow"], before=now)[: max(1, rc["followup_runs"])]
    baseline = _rates(app, prior, node)
    if not baseline["runs"]:  # the reviewed run alone
        with app.ledger.tx() as tx:
            run = tx.get_run(rec["run"])
        baseline = _rates(app, [run] if run else [], None)
    lesson_ids = []
    pfile = pipeline_file(files, rec["flow"]) if files is not None else None
    if pfile is not None:
        for x in lib.for_review(pfile, rec["id"]):
            if node is None or x.node == node:
                lesson_ids.append(x.id)
                with contextlib.suppress(Exception):
                    lib.set_status(pfile, rec["flow"], x.id, "adopted")
    out = {
        "id": aid,
        "review": rec["id"],
        "index": index,
        "run": rec["run"],
        "flow": rec["flow"],
        "pipeline": str(a.get("path") or "").removesuffix(".yaml") or rec.get("pipeline"),
        "path": a.get("path"),
        "node": node,
        "component": next(
            (r["component"] for r in rec.get("rows") or [] if r.get("node") == node), None
        ),
        "summary": str(a.get("summary") or "")[:300],
        "at": now,
        "commit": commit,
        "before": before,
        "lessons": lesson_ids,
        "baseline": baseline,
        "after": {"runs": 0, "trouble": 0.0, "cost_usd": 0.0},
        "verdict": None,
        "detail": "",
        "actions": [],
        "created_at": now,
    }
    store.adoptions.save(out)
    rec.setdefault("adopted", {})[str(index)] = aid
    store.save(rec)
    return out


def undo_card(ad: dict[str, Any], why: str) -> dict[str, Any] | None:
    """A pipeline card that puts the edit's files back as they were (a file the edit added
    stays: a pipeline action can't delete one)."""
    before = ad.get("before") or {}
    if not before.get("yaml") or not ad.get("path"):
        return None
    return {
        "type": "pipeline",
        "path": ad["path"],
        "summary": f"undo: {ad.get('summary') or 'the review edit'}",
        "yaml": before["yaml"],
        "files": {k: v for k, v in (before.get("files") or {}).items() if v is not None},
        "why": why,
    }


def _checked(files: Any, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from agentline import assistant

    if files is None:
        return [{**a, "issues": []} for a in actions]
    return assistant.check_actions(actions, files.root, files.registry, files.config)


def judge(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, str]:
    """helped / hurt / no effect from trouble per run (a change of 30 % and 0.5 at least)."""
    b, a = float(before["trouble"]), float(after["trouble"])
    detail = f"trouble {b:.1f} -> {a:.1f} per run"
    if before.get("cost_usd") or after.get("cost_usd"):
        detail += f", cost ${before.get('cost_usd', 0):.2f} -> ${after.get('cost_usd', 0):.2f}"
    detail += f" over {after['runs']} runs"
    delta = a - b
    if abs(delta) >= 0.5 and abs(delta) >= 0.3 * max(b, a):
        return ("helped" if delta < 0 else "hurt"), detail
    return "no effect", detail


def follow_up(app: Any, files: Any, store: ReviewStore) -> list[dict[str, Any]]:
    """Give a verdict to adoptions with enough runs since (no model call). Returns the ones
    decided now."""
    cfg = getattr(files, "config", None)
    n = max(1, config(cfg)["followup_runs"])
    out = []
    for ad in store.adoptions.list():
        if ad.get("verdict"):
            continue
        later = _finished_runs(app, ad["flow"], after=float(ad["at"]))
        after = _rates(app, later, ad.get("node"))
        if after["runs"] > n:
            after = _rates(app, [r for r in later if r.id in after["run_ids"][:n]], ad.get("node"))
        ad["after"] = after
        if after["runs"] < n:
            store.adoptions.save(ad)
            continue
        verdict, detail = judge(ad["baseline"], after)
        ad.update(verdict=verdict, detail=detail, decided_at=time.time())
        card = undo_card(ad, f"the edit made runs worse ({detail})") if verdict == "hurt" else None
        if card is not None:
            ad["actions"] = _checked(files, [card])
        pfile = pipeline_file(files, ad["flow"]) if files is not None else None
        if pfile is not None:
            for lid in ad.get("lessons") or []:
                with contextlib.suppress(Exception):
                    lib.set_status(pfile, ad["flow"], int(lid), verdict, detail)
        store.adoptions.save(ad)
        out.append(ad)
    return out


# -- finding runs to review and the loop ------------------------------------------------------
def auto_enabled() -> bool:
    """Automatic review is on unless AGENTLINE_REVIEW=off (tests and harnesses set it)."""
    return os.environ.get("AGENTLINE_REVIEW", "").strip().lower() not in ("off", "0", "false")


def find_runs(
    app: Any,
    store: ReviewStore,
    since: float,
    cfg: Any = None,
    *,
    triage_store: Any = None,
    seen: set[str] | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Runs that ended since `since`, have no review yet and (per `[review] auto`) show
    signals. A dead run waits for its triage (up to TRIAGE_GRACE). Returns their signals.
    `seen` remembers runs already looked at that had nothing to review."""
    from agentline.triage import TriageStore
    from agentline.triage import auto_enabled as triage_on

    mode = config(cfg)["auto"]
    if mode == "off":
        return []
    seen = seen if seen is not None else set()
    now = now if now is not None else time.time()
    tstore = triage_store if triage_store is not None else TriageStore()
    with app.ledger.tx() as tx:
        runs = [
            r
            for r in tx.list_runs(None, 200)
            if r.updated_at >= since and r.status in REVIEWED_RUN and r.id not in seen
        ]
    out = []
    for r in runs:
        if now - r.updated_at < SETTLE or store.get(review_id(r.id)) is not None:
            continue
        if r.status == "dead" and triage_on():
            recs = tstore.list(r.id)
            waiting = any(t.get("status") == "running" for t in recs)
            if (waiting or not recs) and now - r.updated_at < TRIAGE_GRACE:
                continue
        sig = signals(app, r.id, cfg=cfg, triage_store=tstore)
        if sig is None:
            continue
        if mode == "all" or has_signals(sig):
            out.append(sig)
        else:
            seen.add(r.id)
    return out


async def run_review_loop(
    app: Any,
    files: Any,
    stop: asyncio.Event,
    *,
    store: ReviewStore | None = None,
    interval: float = LOOP_INTERVAL,
) -> None:
    """Review finished runs one at a time, probe their best proposals, and give adopted edits
    their verdicts, until `stop`."""
    from agentline import probe

    store = store or ReviewStore()
    since = store.since()
    seen: set[str] = set()
    while not stop.is_set():
        try:
            cfg = getattr(files, "config", None)
            found = await asyncio.to_thread(find_runs, app, store, since, cfg, seen=seen)
            for sig in found:
                if stop.is_set():
                    break
                rec = await review(app, files, sig["run"], store=store, sig=sig)
                log.info("review %s of run %s: %s", rec["id"], sig["run"], rec.get("status"))
                for i in probe_candidates(rec, cfg) if rec.get("status") == "done" else []:
                    if stop.is_set():
                        break
                    await probe.probe(app, files, rec["id"], i, store=store)
            await asyncio.to_thread(follow_up, app, files, store)
        except Exception as e:  # never let reviews take the server down
            log.warning("review loop: %s", e)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval)


def chat_text(rec: dict[str, Any]) -> str:
    """A finished review as a chat-app message."""
    from agentline.assistant import title

    lines = [f"📝 Review of {rec['flow']} run {rec['run']}", rec.get("summary") or ""]
    props = [title(a) for a in rec.get("actions") or [] if not a.get("issues")]
    if props:
        lines.append("Proposed: " + "; ".join(props) + ". Apply them in Agentline Studio.")
    elif rec.get("keep"):
        lines.append("Nothing to change.")
    return "\n".join(x for x in lines if x)
