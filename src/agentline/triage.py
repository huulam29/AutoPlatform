"""Failure triage: when a run goes wrong, decide whose fault it is and propose what to change.

Trigger: automatic (`run_triage_loop`, run by `agentline serve`) or on click (API, Chat). A
failure is an execution that ended dead or needs-review, a run that ended dead / needs-review
without one, or an agent node that finished with an empty result. One triage per failure: the
key is the execution id plus its manual retry count, so a node that fails again after a retry
gets a new one. Failures from before the loop first ran are left alone (click to triage them).

The Chat's harness gets the evidence "Fix this" collects (fixit.py: errors, journal, transcript
tail) plus the pipeline file and the node's prompt / schema files, and answers with a verdict:
  ai-instruction   the pipeline's instructions led the agent (or a script step) wrong
  infrastructure   agentline itself misbehaved
  external         a third-party outage, a missing or revoked key, a CLI that isn't installed
with its reasoning and proposals, checked like Chat proposals (assistant.check_actions):
  ai-instruction   a pipeline edit + a `lesson` line for the Chat instructions
  infrastructure   a redacted report on the run's local issue, and a GitHub issue when
                   `[report] github_repo` and GITHUB_TOKEN are set (a `report_issue` card, or
                   created at once with `github_auto = true`); otherwise "Copy as GitHub issue"
  external         keys / settings to set
and a `retry` of the failed node. Nothing in a pipeline changes here: every fix is a card the
user applies (decision 63).

Costs: the triage call is a cost of the failed run (note "triage") and counts against its
`budget_usd`; triage is skipped while the run's budget is used up.

App problems: crash / error reports (incidents) are agentline's own problems by definition, so
each one gets an `infrastructure` record without a model call (`triage_incidents`): a redacted
report from the incident and the same GitHub card / auto-report. They and the infrastructure
verdicts on runs are listed on App reports (`app_reports`), not on Problems (decision 64).

Records: `$AGENTLINE_HOME/triage/<id>.json` (and `state.json`: when automatic triage began).
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError

log = logging.getLogger("agentline.triage")

VERDICTS = ("ai-instruction", "infrastructure", "external")
FAILED_EXEC = ("dead", "needs-review")
FAILED_RUN = ("dead", "needs-review")
MAX_EVIDENCE = 24_000
MAX_FILE = 12_000
MAX_LESSON = 300
DEFAULT_LABELS = ("agentline", "triage")
LOOP_INTERVAL = 10.0
_GITHUB_MARK = "agentline-triage"


def triage_dir() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "triage"


# -- GitHub reporting config ------------------------------------------------------------------
def report_config(cfg: Any) -> dict[str, Any]:
    """`[report]` in agentline.toml: github_repo = "owner/name", github_auto, labels."""
    from agentline.integrations.github import split_repo

    raw = dict(((getattr(cfg, "raw", None) or {}).get("report")) or {})
    repo = str(raw.get("github_repo") or "").strip()
    error = None
    if repo:
        try:
            split_repo(repo)
        except ValueError as e:
            error, repo = str(e), ""
    labels = raw.get("labels")
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        labels = list(DEFAULT_LABELS)
    return {
        "github_repo": repo,
        "github_auto": bool(raw.get("github_auto")),
        "labels": labels,
        "error": error,
    }


def github_token() -> str:
    """GITHUB_TOKEN (env, then the secrets file), else the GitHub CLI's login."""
    from agentline.github_setup import token

    return token()[0]


def github_status(cfg: Any) -> dict[str, Any]:
    """What the UI shows: can a triage report go to GitHub? Never the token."""
    rc = report_config(cfg)
    token_set = bool(github_token())
    return {
        "repo": rc["github_repo"] or None,
        "token_set": token_set,
        "ready": bool(rc["github_repo"]) and token_set,
        "auto": rc["github_auto"],
        "error": rc["error"],
    }


# -- records ----------------------------------------------------------------------------------
@dataclass
class Failure:
    run_id: str
    flow: str
    node: str | None
    execution: str | None
    trigger: str  # dead | needs-review | run-dead | run-needs-review | empty-output
    key: str

    @property
    def id(self) -> str:
        return "t" + hashlib.sha1(self.key.encode()).hexdigest()[:12]


class TriageStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else triage_dir()

    def _path(self, tid: str) -> Path:
        if not re.fullmatch(r"t[0-9a-f]{12}", tid):
            raise KeyError(tid)
        return self.root / f"{tid}.json"

    def get(self, tid: str) -> dict[str, Any] | None:
        try:
            return dict(json.loads(self._path(tid).read_text(encoding="utf-8")))
        except (KeyError, OSError, ValueError):
            return None

    def save(self, rec: dict[str, Any]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        rec["updated_at"] = time.time()
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".triage.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, default=str)
        os.replace(tmp, self._path(str(rec["id"])))
        return rec

    def delete(self, tid: str) -> bool:
        try:
            self._path(tid).unlink()
            return True
        except (KeyError, OSError):
            return False

    def list(self, run: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Newest first."""
        if not self.root.exists():
            return []
        out = []
        for p in self.root.glob("t*.json"):
            with contextlib.suppress(OSError, ValueError):
                rec = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(rec, dict) and (run is None or rec.get("run") == run):
                    out.append(rec)
        out.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return out[:limit]

    def since(self) -> float:
        """When automatic triage began in this data folder (set on first use)."""
        p = self.root / "state.json"
        with contextlib.suppress(OSError, ValueError):
            return float(json.loads(p.read_text(encoding="utf-8"))["since"])
        self.root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        p.write_text(json.dumps({"since": now}), encoding="utf-8")
        return now


# -- finding failures -------------------------------------------------------------------------
def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list)):
        return not value or all(
            _empty(v) for v in (value.values() if isinstance(value, dict) else value)
        )
    return False


def failure_for(app: Any, run_id: str, node: str | None = None) -> Failure | None:
    """The failure to triage in a run (a failed execution, of `node` if given), for a click."""
    with app.ledger.tx() as tx:
        run = tx.get_run(run_id)
        if run is None:
            return None
        execs = tx.list_executions(run_id)
    bad = [x for x in execs if x.status in FAILED_EXEC and (node is None or x.node_id == node)]
    if bad:
        x = max(bad, key=lambda e: (e.finished_at or 0, e.iteration))
        return _exec_failure(run, x, x.status)
    if node is not None:
        hit = [x for x in execs if x.node_id == node]
        if not hit:
            return None
        x = max(hit, key=lambda e: (e.finished_at or 0, e.iteration))
        return _exec_failure(run, x, "empty-output" if x.status == "done" else x.status)
    return Failure(run.id, run.flow, None, None, f"run-{run.status}", f"run:{run.id}:{run.status}")


def _exec_failure(run: Any, x: Any, trigger: str) -> Failure:
    retries = int((x.detail or {}).get("retries") or 0)
    return Failure(run.id, run.flow, x.node_id, x.id, trigger, f"{x.id}#{retries}:{trigger}")


def find_failures(
    app: Any, store: TriageStore, since: float, checked: set[str] | None = None
) -> list[Failure]:
    """New failures since `since` that have no triage yet. `checked` remembers done executions
    whose output was already looked at (so a result is loaded once)."""
    checked = checked if checked is not None else set()
    out: list[Failure] = []
    with app.ledger.tx() as tx:
        runs = [r for r in tx.list_runs(None, 200) if r.updated_at >= since]
        for run in runs:
            execs = tx.list_executions(run.id)
            found = False
            for x in execs:
                if x.status in FAILED_EXEC and (x.finished_at or run.updated_at) >= since:
                    out.append(_exec_failure(run, x, x.status))
                    found = True
                elif x.status == "done" and x.id not in checked and x.result_ref:
                    checked.add(x.id)
                    if (x.finished_at or 0) < since or not _is_agent(app, run.flow, x.node_id):
                        continue
                    with contextlib.suppress(Exception):
                        if _empty(app.scheduler.artifacts.load(tx, x.result_ref)):
                            out.append(_exec_failure(run, x, "empty-output"))
            if run.status in FAILED_RUN and not found:
                key = f"run:{run.id}:{run.status}"
                out.append(Failure(run.id, run.flow, None, None, f"run-{run.status}", key))
    return [f for f in out if store.get(f.id) is None]


def _is_agent(app: Any, flow: str, node: str) -> bool:
    try:
        return app.graph(flow).nodes[node].meta.get("type") == "agent"
    except Exception:
        return False


# -- evidence and the prompt ------------------------------------------------------------------
GUIDE = """\
You are agentline's failure triage. agentline is a self-hosted engine that runs multi-agent
pipelines described in YAML (agent / action / gate / router / wait nodes). A pipeline run went
wrong. Decide whose fault it is, from the EVIDENCE:

- "ai-instruction": the pipeline's own instructions caused it, so editing the pipeline fixes
  it: a vague or wrong prompt, a missing or too strict output_schema, too few max_turns,
  allowed_tools missing a tool the task needs, the wrong harness or model for the job, a template
  using an input that isn't there, or a script step the pipeline author wrote wrong (bad
  command, wrong path, wrong JSON on stdout).
- "infrastructure": agentline itself misbehaved: a traceback inside agentline's code, a wrong
  state transition, a worker or lease problem, a runner that mishandles well-formed output, a
  validator that accepted something it shouldn't. Editing the pipeline wouldn't really fix it.
- "external": neither: a third-party outage, 5xx or rate limit, a missing, expired or revoked
  key, a network problem, a CLI that isn't installed, a quota.

Then propose what fixes it, as actions (they are shown to the user as cards to apply):
- ai-instruction: a "pipeline" action with the WHOLE new YAML for the PIPELINE FILE path and,
  in "files", only the prompt / schema / script files you change (paths relative to the
  pipeline's folder). Keep everything the fix doesn't need to change. And a "lesson": ONE line
  for the Chat instructions, phrased as a general rule for building future pipelines so the
  same mistake isn't made again (e.g. "Give agent nodes whose output later nodes read an
  output_schema listing every field they use."). Not about this run's names or data.
- infrastructure: a "report" for agentline's maintainers: a title and a markdown body (what
  happened, what was expected, the suspect part of agentline, how to reproduce). No secrets.
- external: "key" actions for keys to set (never a value) or "setting" actions.
- Always: a "retry" action for the run and the failed node when retrying makes sense after the
  fix (or right away, for a transient outage).

Reply with ONE JSON object and nothing else:
{
  "verdict": "ai-instruction" | "infrastructure" | "external",
  "summary": "one line: what went wrong",
  "reasoning": "a few sentences: why this verdict, citing the evidence",
  "node": "<the failing node id>",
  "lesson": "ai-instruction only: one line",
  "report": {"title": "...", "body": "infrastructure only: markdown"},
  "actions": [
    {"type": "pipeline", "path": "<PIPELINE FILE>", "summary": "...", "yaml": "<whole YAML>",
     "files": {"prompts/plan.md": "..."}},
    {"type": "key", "name": "ENV_VAR", "why": "..."},
    {"type": "setting", "section": "providers" | "models" | "harnesses" | "mcp", "name": "...",
     "body": {...}, "why": "..."},
    {"type": "retry", "run": "<run id>", "node": "<node id>", "why": "..."}
  ]
}
"""


def _pipeline_file(files: Any, flow: str) -> tuple[Path, str] | None:
    if files is None:
        return None
    with contextlib.suppress(Exception):
        hit = files.find_by_pipeline_name(flow)
        if hit:
            path = files.resolve(hit[0])
            return path, path.read_text(encoding="utf-8")
    return None


def _node_files(path: Path, yaml_text: str, node: str | None) -> list[tuple[str, str]]:
    """The node's prompt and output_schema files (when they are files), for the prompt."""
    from ruamel.yaml import YAML

    if not node:
        return []
    try:
        spec = YAML(typ="safe").load(yaml_text) or {}
        n = (spec.get("nodes") or {}).get(node) or {}
    except Exception:
        return []
    out = []
    for field in ("prompt", "output_schema"):
        v = n.get(field)
        if not isinstance(v, str) or "\n" in v or len(v) > 300:
            continue
        p = (path.parent / v).resolve()
        if p.is_file() and p.is_relative_to(path.parent.resolve()):
            with contextlib.suppress(OSError):
                out.append((v, p.read_text(encoding="utf-8")[:MAX_FILE]))
    return out


def evidence(app: Any, files: Any, f: Failure) -> dict[str, Any]:
    """{text, pipeline (editor name), path (relative to the pipelines folder), yaml}."""
    from agentline import fixit

    found = fixit.for_run(app, f.run_id)
    lines = [found["text"] if found else f"Run {f.run_id} of {f.flow}."]
    if f.trigger == "empty-output":
        lines.append(f"\nNode {f.node} finished but its result is empty (no usable output).")
    text = app.redactor.redact("\n".join(lines))[-MAX_EVIDENCE:]
    out: dict[str, Any] = {"text": text, "pipeline": None, "path": None, "yaml": None, "files": []}
    hit = _pipeline_file(files, f.flow)
    if hit:
        path, yaml_text = hit
        out["pipeline"] = files.name_of(path)
        out["path"] = out["pipeline"] + ".yaml"
        out["yaml"] = yaml_text
        out["files"] = _node_files(path, yaml_text, f.node)
    return out


def build_prompt(f: Failure, ev: dict[str, Any], instructions: str) -> str:
    parts = [
        GUIDE,
        f"## FAILURE\nRun {f.run_id} of pipeline {f.flow}; node {f.node or '(the run)'};"
        f" trigger: {f.trigger}.",
        "## EVIDENCE\n" + ev["text"],
    ]
    if ev.get("yaml") is not None:
        parts.append(f"## PIPELINE FILE {ev['path']}\n{ev['yaml']}")
        for rel, text in ev.get("files") or []:
            parts.append(f"## FILE {rel} (relative to the pipeline's folder)\n{text}")
    else:
        parts.append("## PIPELINE FILE\n(not found in the pipelines folder: no pipeline action)")
    parts.append("## CURRENT CHAT INSTRUCTIONS (the lesson is appended here)\n" + instructions)
    return "\n\n".join(parts) + "\n\nReply with the JSON object."


def parse_verdict(output: Any) -> dict[str, Any]:
    from agentline.assistant import _json_object

    data = output if isinstance(output, dict) else _json_object(str(output or ""))
    if not data:
        raise ValidationError("the triage agent didn't answer with a JSON object")
    verdict = str(data.get("verdict") or "").strip().lower().replace("_", "-")
    if verdict not in VERDICTS:
        raise ValidationError(f"the triage agent gave no known verdict ({verdict or 'none'})")
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    lesson = " ".join(str(data.get("lesson") or "").split())[:MAX_LESSON]
    return {
        "verdict": verdict,
        "summary": " ".join(str(data.get("summary") or "").split())[:300],
        "reasoning": str(data.get("reasoning") or "").strip()[:4000],
        "node": str(data.get("node") or "") or None,
        "lesson": lesson if verdict == "ai-instruction" else "",
        "report": {
            "title": str(report.get("title") or "")[:200],
            "body": str(report.get("body") or "")[:8000],
        },
        "actions": [a for a in (data.get("actions") or []) if isinstance(a, dict)],
    }


# -- the report -------------------------------------------------------------------------------
def fingerprint(f: Failure, error: str) -> str:
    """Same pipeline, node and first error line -> same fingerprint (one GitHub issue)."""
    first = next((ln for ln in error.splitlines() if ln.strip()), "")
    first = re.sub(r"\b[0-9a-f]{8,}\b|\d+", "#", first)[:200]
    return hashlib.sha1(f"{f.flow}|{f.node}|{first}".encode()).hexdigest()[:12]


def report_markdown(rec: dict[str, Any], evidence_text: str) -> str:
    """The issue body (local report and GitHub), already redacted by the caller."""
    from agentline import __version__

    rep = rec.get("report") or {}
    lines = [
        f"**Pipeline** `{rec['flow']}` · **Run** `{rec['run']}`"
        f" · **Node** `{rec.get('node') or '-'}` · **Trigger** {rec['trigger']}"
        f" · **agentline** {__version__}",
        "",
        "## What happened",
        rec.get("summary") or "",
        "",
        "## Why triage thinks it's agentline",
        rec.get("reasoning") or "",
    ]
    if rep.get("body"):
        lines += ["", "## Report", rep["body"]]
    lines += [
        "",
        "<details><summary>Evidence (redacted)</summary>",
        "",
        "```text",
        evidence_text[-6000:].replace("```", "'''"),
        "```",
        "</details>",
        "",
        f"<!-- {_GITHUB_MARK}: {rec.get('fingerprint')} -->",
    ]
    return "\n".join(lines)


def report_title(rec: dict[str, Any]) -> str:
    title = (rec.get("report") or {}).get("title") or rec.get("summary") or "pipeline failure"
    return f"[triage] {title}"[:200]


# -- one triage -------------------------------------------------------------------------------
_inflight: set[str] = set()


def _chat_harness() -> tuple[str, str | None]:
    from agentline import setup

    saved = setup.read_state().get("assistant") or {}
    from agentline import agents as ai

    return str(saved.get("harness") or ai.FALLBACK_HARNESS), saved.get("model") or None


def _record(f: Failure, manual: bool) -> dict[str, Any]:
    now = time.time()
    return {
        "id": f.id,
        "key": f.key,
        "run": f.run_id,
        "flow": f.flow,
        "node": f.node,
        "execution": f.execution,
        "trigger": f.trigger,
        "manual": manual,
        "status": "running",
        "verdict": None,
        "summary": "",
        "reasoning": "",
        "lesson": "",
        "report": None,
        "actions": [],
        "issue": None,
        "github": None,
        "github_error": None,
        "fingerprint": None,
        "pipeline": None,
        "harness": None,
        "model": None,
        "cost_usd": 0.0,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }


def _budget_left(app: Any, run_id: str) -> tuple[bool, str]:
    with app.ledger.tx() as tx:
        run = tx.get_run(run_id)
    if run is None or run.budget_usd is None:
        return True, ""
    if run.cost_usd >= run.budget_usd - 1e-9:
        return False, (
            f"the run's budget is used up (${run.cost_usd:.2f} of ${run.budget_usd:.2f});"
            " triage counts against it"
        )
    return True, ""


def _charge(app: Any, f: Failure, usd: float) -> None:
    """The triage call is a cost of the failed run (and counts against its budget)."""
    if usd <= 0:
        return
    with app.ledger.tx() as tx:
        exec_id = f.execution
        attempt = 1
        if exec_id is None:
            execs = tx.list_executions(f.run_id)
            if not execs:
                return
            last = max(execs, key=lambda e: e.finished_at or e.started_at or 0)
            exec_id, attempt = last.id, last.attempt
        else:
            x = tx.get_execution(exec_id)
            attempt = x.attempt if x else 1
        tx.add_cost(f.run_id, exec_id, attempt, usd, "triage", touch=False)


def _with_cards(app: Any, f: Failure, rec: dict[str, Any], got: dict[str, Any]) -> list[Any]:
    """The agent's proposals plus the cards triage always offers."""
    actions = list(got["actions"])
    for a in actions:
        if a.get("type") == "retry":
            a.setdefault("run", f.run_id)
    if rec["verdict"] != "ai-instruction":
        actions = [a for a in actions if a.get("type") != "pipeline"]
    if rec["lesson"]:
        actions.append(
            {
                "type": "lesson",
                "text": rec["lesson"],
                "why": "so new pipelines avoid the same mistake",
            }
        )
    has_retry = any(a.get("type") == "retry" for a in actions)
    if f.node and f.trigger in FAILED_EXEC and not has_retry:
        actions.append(
            {
                "type": "retry",
                "run": f.run_id,
                "node": f.node,
                "why": "rerun the failed node once the fix is in",
            }
        )
    return actions


async def triage(
    app: Any,
    files: Any,
    f: Failure,
    *,
    store: TriageStore | None = None,
    issues: Any = None,
    manual: bool = False,
    force: bool = False,
    live: Any = None,
) -> dict[str, Any]:
    """Triage one failure and save the record (also when it fails or is skipped). `force`
    redoes one that exists; a click on a used-up budget is still skipped."""
    from agentline import assistant, chat_instructions

    store = store or TriageStore()
    old = store.get(f.id)
    if old is not None and not force and old.get("status") != "failed":
        return old
    if f.key in _inflight:
        return old or {**_record(f, manual), "status": "running"}
    _inflight.add(f.key)
    try:
        rec = _record(f, manual)
        if old is not None:
            rec["created_at"] = old.get("created_at", rec["created_at"])
            rec["github"] = old.get("github")
        ok, why = await asyncio.to_thread(_budget_left, app, f.run_id)
        if not ok:
            rec.update(status="skipped", error=why)
            return store.save(rec)
        store.save(rec)
        harness, model = _chat_harness()
        rec.update(harness=harness, model=model)
        ev = await asyncio.to_thread(evidence, app, files, f)
        rec["pipeline"] = ev["pipeline"]
        error_text = ev["text"]
        rec["fingerprint"] = fingerprint(f, _first_error(app, f) or error_text)
        prompt = build_prompt(f, ev, chat_instructions.read())
        try:
            output, cost = await assistant._ask(files, harness, model, prompt)
        except assistant.AssistantError as e:
            rec.update(status="failed", error=str(e))
            return store.save(rec)
        rec["cost_usd"] = float(cost or 0.0)
        await asyncio.to_thread(_charge, app, f, rec["cost_usd"])
        try:
            got = app.redactor.redact_value(parse_verdict(output))  # nothing secret is kept
        except ValidationError as e:
            rec.update(status="failed", error=str(e))
            return store.save(rec)
        rec.update({k: got[k] for k in ("verdict", "summary", "reasoning", "lesson")})
        rec["node"] = f.node or got["node"]
        if rec["verdict"] == "infrastructure":
            rec["report"] = {
                "title": got["report"]["title"] or rec["summary"],
                "body": got["report"]["body"],
            }
            rec["report"]["markdown"] = app.redactor.redact(report_markdown(rec, error_text))
        actions = _with_cards(app, f, rec, got)
        if files is not None:
            if live is None:
                live = assistant.gather_live(files, app, issues)
            actions = assistant.check_actions(
                actions, files.root, files.registry, files.config, live
            )
        else:
            actions = [{**a, "issues": []} for a in actions]
        rec["actions"] = actions
        rec["status"] = "done"
        store.save(rec)
        _link_issue(app, f, rec, issues)
        cfg = getattr(files, "config", None)
        if rec["verdict"] == "infrastructure":
            gh = github_status(cfg)
            if gh["ready"]:
                rec["actions"].append(
                    {
                        "type": "report_issue",
                        "triage": rec["id"],
                        "issues": [],
                        "why": f"report it on {gh['repo']}",
                    }
                )
                if gh["auto"] and not rec.get("github"):
                    with contextlib.suppress(Exception):
                        rec = await asyncio.to_thread(report_github, rec, cfg, store, issues)
        return store.save(rec)
    finally:
        _inflight.discard(f.key)


def _first_error(app: Any, f: Failure) -> str:
    with app.ledger.tx() as tx:
        if f.execution:
            x = tx.get_execution(f.execution)
            return (x.error or x.reason or "") if x else ""
        run = tx.get_run(f.run_id)
        return (run.error or "") if run else ""


def _link_issue(app: Any, f: Failure, rec: dict[str, Any], issues: Any) -> None:
    """Show the verdict on Problems: the run's issue (created for a failed run anyway, and for
    an infrastructure verdict on a run that didn't fail) carries the triage."""
    if issues is None:
        return
    with app.ledger.tx() as tx:
        run = tx.get_run(f.run_id)
    if run is None or (run.status not in FAILED_RUN and rec["verdict"] != "infrastructure"):
        return
    issue, _ = issues.ensure(
        "run",
        run.id,
        title=f"Run {run.id} of {run.flow}: {run.status}",
        seen_at=float(run.updated_at),
        detail=str(run.error or ""),
    )
    note = f"Triage: {rec['verdict']}: {rec['summary']}"
    if rec.get("report"):
        note += " (report saved with the triage)"
    issues.set_triage(
        issue["number"],
        {
            "id": rec["id"],
            "verdict": rec["verdict"],
            "summary": rec["summary"],
            "github": rec.get("github"),
        },
        note,
    )
    rec["issue"] = issue["number"]


def report_github(
    rec: dict[str, Any], cfg: Any, store: TriageStore, issues: Any = None, client: Any = None
) -> dict[str, Any]:
    """Create the GitHub issue for an infrastructure triage (once). A failure with the same
    fingerprint that already has one gets a comment there instead of a new issue."""
    from agentline.integrations.github import GitHubClient
    from agentline.integrations.http import ApiError

    if rec.get("github"):
        return rec
    if rec.get("verdict") != "infrastructure" or not rec.get("report"):
        raise ValidationError("only an infrastructure triage has a report to send")
    st = github_status(cfg)
    if not st["ready"]:
        raise ValidationError(
            'GitHub reporting isn\'t set up: put github_repo = "owner/name" under [report] in'
            " agentline.toml and set the GITHUB_TOKEN key"
            + (f" ({st['error']})" if st["error"] else "")
        )
    rc = report_config(cfg)
    client = client or GitHubClient(github_token(), os.environ.get("GITHUB_API_URL") or "")
    same = next(
        (
            r
            for r in store.list()
            if r.get("fingerprint") == rec.get("fingerprint")
            and r.get("github")
            and r["id"] != rec["id"]
        ),
        None,
    )
    try:
        if same:
            gh = same["github"]
            client.comment_issue(
                gh["repo"],
                gh["number"],
                (
                    f"Happened again: run `{rec['run']}` of `{rec['flow']}`.\n\n"
                    if rec.get("run")
                    else "Happened again.\n\n"
                )
                + rec["report"]["markdown"],
            )
            rec["github"] = {**gh, "comment": True}
        else:
            made = client.create_issue(
                rc["github_repo"],
                title=report_title(rec),
                body=rec["report"]["markdown"],
                labels=rc["labels"],
            )
            rec["github"] = {
                "repo": rc["github_repo"],
                "number": made.get("number"),
                "url": made.get("html_url") or made.get("url"),
            }
        rec["github_error"] = None
    except ApiError as e:
        rec["github_error"] = str(e)
        store.save(rec)
        raise
    store.save(rec)
    if issues is not None and rec.get("issue"):
        with contextlib.suppress(KeyError):
            issues.set_triage(
                rec["issue"],
                {
                    "id": rec["id"],
                    "verdict": rec["verdict"],
                    "summary": rec["summary"],
                    "github": rec["github"],
                },
                f"Reported on GitHub: {rec['github'].get('url')}",
            )
    return rec


def chat_text(rec: dict[str, Any]) -> str:
    """The verdict as a chat-app message (Telegram / Slack / Discord)."""
    from agentline.assistant import title

    where = f" at {rec['node']}" if rec.get("node") else ""
    head = "an agentline crash report"
    if rec.get("run"):
        head = f"{rec['flow']} run {rec['run']}{where}"
    lines = [
        f"🔎 Triage of {head}: {rec['verdict']}",
        rec.get("summary") or "",
    ]
    if rec.get("github", {}) and rec["github"].get("url"):
        lines.append(f"Reported: {rec['github']['url']}")
    props = [title(a) for a in rec.get("actions") or [] if not a.get("issues")]
    if props:
        lines.append("Proposed: " + "; ".join(props) + ". Apply them in Agentline Studio.")
    return "\n".join(x for x in lines if x)


# -- app problems (incidents) -----------------------------------------------------------------
def incident_id(iid: str) -> str:
    return "t" + hashlib.sha1(f"incident:{iid}".encode()).hexdigest()[:12]


def incident_markdown(inc: dict[str, Any]) -> str:
    """The issue body for a crash / error report (the incident is redacted when recorded;
    the caller redacts again)."""
    from agentline import __version__

    ctx = inc.get("context") or {}
    lines = [
        f"**Source** {inc.get('source', '?')} · **Kind** {inc.get('kind', '?')}"
        f" · **Seen** {inc.get('count', 1)}x · **agentline** {inc.get('version') or __version__}"
        f" · **Platform** {inc.get('platform') or '?'}",
        "",
        "## What happened",
        str(inc.get("message") or inc.get("title") or ""),
    ]
    if inc.get("traceback"):
        tb = str(inc["traceback"])[-6000:].replace("```", "'''")
        lines += ["", "## Traceback", "", "```text", tb, "```"]
    if ctx:
        ctx_text = json.dumps(ctx, indent=2, default=str)[:3000]
        lines += ["", "## Context", "", "```json", ctx_text, "```"]
    lines += ["", f"<!-- {_GITHUB_MARK}: incident {inc.get('id')} -->"]
    return "\n".join(lines)


def triage_incidents(
    incidents: Any, store: TriageStore, cfg: Any, redactor: Any = None
) -> list[dict[str, Any]]:
    """An infrastructure record for every crash / error report, new or seen again (no model
    call: an incident is agentline's own problem). Records whose report was deleted go too.
    Returns the records made or updated."""
    out = []
    items = incidents.list(1000)
    ids = {incident_id(str(i["id"])) for i in items if i.get("id")}
    for rec in store.list():
        if rec.get("incident") and rec["id"] not in ids:
            store.delete(rec["id"])
    gh = github_status(cfg)
    for summary in items:
        iid = str(summary.get("id") or "")
        if not iid:
            continue
        tid = incident_id(iid)
        old = store.get(tid) or {}
        count = int(summary.get("count") or 1)
        if old and int(old.get("incident_count") or 0) >= count:
            continue
        inc = incidents.get(iid) or summary
        md = incident_markdown(inc)
        if redactor is not None:
            md = redactor.redact(md)
        title = str(inc.get("title") or "crash report")
        rec = {
            **old,
            "id": tid, "key": f"incident:{iid}", "incident": iid, "incident_count": count,
            "run": None, "flow": "agentline", "node": None, "execution": None,
            "trigger": str(inc.get("kind") or "incident"), "manual": False, "status": "done",
            "verdict": "infrastructure", "summary": title[:300],
            "reasoning": f"A {inc.get('kind', 'crash')} report from agentline itself (the"
            f" {inc.get('source', 'engine')}): not something a pipeline or setting change fixes.",
            "lesson": "", "issue": None, "pipeline": None, "harness": None, "model": None,
            "cost_usd": 0.0, "error": None, "fingerprint": f"incident-{iid}",
            "report": {"title": title[:200], "body": "", "markdown": md},
            "actions": [], "github": old.get("github"), "github_error": old.get("github_error"),
            "created_at": old.get("created_at", time.time()),
        }  # fmt: skip
        if gh["ready"] and not rec["github"]:
            rec["actions"] = [{"type": "report_issue", "triage": tid, "issues": [],
                               "why": f"report it on {gh['repo']}"}]  # fmt: skip
        store.save(rec)
        if gh["ready"] and gh["auto"] and not rec["github"]:
            with contextlib.suppress(Exception):
                rec = report_github(rec, cfg, store)
        out.append(rec)
    return out


def app_reports(
    store: TriageStore, incidents: Any = None, cfg: Any = None, redactor: Any = None
) -> list[dict[str, Any]]:
    """App reports: crash / error reports and infrastructure verdicts on runs, newest first."""
    if incidents is not None:
        triage_incidents(incidents, store, cfg, redactor)
    return [r for r in store.list() if r.get("verdict") == "infrastructure"]


# -- the loop ---------------------------------------------------------------------------------
def auto_enabled() -> bool:
    """Automatic triage is on unless AGENTLINE_TRIAGE=off (tests and harnesses set it)."""
    return os.environ.get("AGENTLINE_TRIAGE", "").strip().lower() not in ("off", "0", "false")


async def run_triage_loop(
    app: Any,
    files: Any,
    stop: asyncio.Event,
    *,
    store: TriageStore | None = None,
    issues: Any = None,
    incidents: Any = None,
    interval: float = LOOP_INTERVAL,
) -> None:
    """Triage new failures one at a time (and every new crash report) until `stop`."""
    store = store or TriageStore()
    since = store.since()
    checked: set[str] = set()
    while not stop.is_set():
        try:
            if incidents is not None:
                cfg = getattr(files, "config", None)
                await asyncio.to_thread(triage_incidents, incidents, store, cfg, app.redactor)
            found = await asyncio.to_thread(find_failures, app, store, since, checked)
            for f in found:
                if stop.is_set():
                    break
                rec = await triage(app, files, f, store=store, issues=issues)
                log.info(
                    "triage %s of run %s: %s",
                    rec["id"],
                    f.run_id,
                    rec.get("verdict") or rec.get("status"),
                )
        except Exception as e:  # never let triage take the server down
            log.warning("triage loop: %s", e)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval)
