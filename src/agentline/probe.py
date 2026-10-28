"""Probes: replay one agent node with a review's proposed edit and compare (phase 38).

Harness² tries an edit on the same task and judges it by the difference in behavior, without a
ground-truth score. A probe does that for one proposal of a run review:

- **Which node.** The agent node the edit changes (the proposal's `node`, else the first
  changed agent node that ran). Of its executions: a failed one, else iteration 1 when the node
  looped (later iterations already had a reviewer's feedback), else the only one.
- **Inputs.** What that execution read, from lineage (`read` links), so a probe of iteration 2
  gets iteration 2's inputs; `for_each` nodes replay their first task's item.
- **Workspace.** A coding node runs in a throwaway `git clone` of the local mirror at the commit
  the execution started from (`start_sha`), with the `origin` remote removed so nothing can be
  pushed, under `<home>/probes/<id>/`, deleted afterwards. The run's branch is never touched.
  Container nodes are replayed in that local clone too (no container): a tool the image had
  and the host lacks shows up as a probe failure.
- **Isolation.** Only the agent runs (no actions); MCP servers are off unless marked
  `probe_safe = true`; env names for GitHub / Jira / chat apps are not passed; the workspace's
  `secrets` aren't either.
- **Compare.** The Chat's harness sees the original and the replay as anonymous A / B with the
  problem the review observed, and says which handles it better. Shown as better / worse /
  same, "1 run": one sample, never proof.

Costs: the replay and the comparison count against the run's budget (note "probe") and against
`[review] probe_budget_usd` per review. A probe is skipped when either is used up.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError

MAX_SHOW = 6_000
NO_ENV = ("GITHUB", "GH_", "JIRA", "SLACK", "DISCORD", "TELEGRAM")
COMPARED = (
    "prompt",
    "output_schema",
    "skills",
    "mcp",
    "model",
    "harness",
    "runner",
    "allowed_tools",
    "max_turns",
    "params",
    "workspace",
)

COMPARE_GUIDE = """\
You compare two executions of the same agent node of an agentline pipeline, on the same
inputs. One ran with the pipeline's instructions as they were, the other with an edit a run
review proposed; you aren't told which is which. The review observed a PROBLEM in the
original run. Judge only from what each execution produced (output, code diff, schema errors,
transcript): which one handles the PROBLEM better, without making anything else worse?
"same" is a real answer when neither is clearly better.

Reply with ONE JSON object and nothing else:
{"better": "A" | "B" | "same", "reasoning": "a few sentences citing what each produced"}
"""


class ProbeError(Exception):
    """A probe that can't run: `status` is skipped (nothing spent) or failed."""

    def __init__(self, message: str, status: str = "failed") -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Target:
    run_id: str
    flow: str
    node: str
    execution: Any
    old: Any  # NodeSpec as it ran
    new: Any  # NodeSpec with the edit
    defaults: Any
    inputs: dict[str, Any]
    context: dict[str, Any]
    workspace: dict[str, Any] | None
    baseline: dict[str, Any]
    notes: list[str] = field(default_factory=list)


def probes_dir() -> Path:
    from agentline.workspaces.git import agentline_home

    return agentline_home() / "probes"


# -- what to replay ---------------------------------------------------------------------------
def _spec_of(yaml_text: str, path: str, files: Any, extra: dict[str, str]) -> Any:
    """The proposed pipeline parsed in a scratch copy of its folder (as check_pipeline)."""
    from agentline.pipeline.spec import parse_spec

    target_dir = (files.root / path).parent
    with tempfile.TemporaryDirectory(prefix="agentline-probe-spec-") as tmp:
        scratch = Path(tmp)
        if target_dir != files.root and target_dir.is_dir():
            shutil.copytree(target_dir, scratch, dirs_exist_ok=True)
        else:
            for sub in ("prompts", "schemas"):
                if (target_dir / sub).is_dir():
                    shutil.copytree(target_dir / sub, scratch / sub, dirs_exist_ok=True)
        for rel, text in extra.items():
            (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
            (scratch / rel).write_text(text)
        p = scratch / Path(path).name
        p.write_text(yaml_text)
        return parse_spec(yaml_text, path=p, base_dir=scratch)


def changed_agent_nodes(old: Any, new: Any) -> list[str]:
    out = []
    for nid, n in new.nodes.items():
        if n.type != "agent":
            continue
        o = old.nodes.get(nid)
        if o is None or any(getattr(o, k) != getattr(n, k) for k in COMPARED):
            out.append(nid)
    return out


def pick_execution(execs: list[Any], node: str) -> Any | None:
    mine = sorted((x for x in execs if x.node_id == node), key=lambda x: x.iteration)
    if not mine:
        return None
    bad = [x for x in mine if x.status in ("dead", "needs-review", "failed")]
    if bad:
        return bad[-1]
    return mine[0]


def target_for(app: Any, files: Any, rec: dict[str, Any], a: dict[str, Any]) -> Target:
    """Everything a replay needs (sync: reads the ledger and parses specs)."""
    from agentline.pipeline.template import MISSING, lookup

    if a.get("type") != "pipeline":
        raise ProbeError("only a pipeline edit can be probed", "skipped")
    if a.get("issues"):
        raise ProbeError("the proposal doesn't validate: fix it before probing", "skipped")
    try:
        graph = app.graph(rec["flow"])
    except Exception:
        raise ProbeError(f"pipeline {rec['flow']} isn't loaded", "skipped") from None
    old = graph.meta.get("spec")
    try:
        new = _spec_of(str(a.get("yaml") or ""), str(a.get("path") or ""), files,
                       dict(a.get("files") or {}))  # fmt: skip
    except Exception as e:  # a SpecError, a YAML syntax error and the like
        raise ProbeError(f"the edited pipeline doesn't parse: {e}", "skipped") from None
    changed = changed_agent_nodes(old, new)
    with app.ledger.tx() as tx:
        run = tx.get_run(rec["run"])
        if run is None:
            raise ProbeError(f"no run {rec['run']}", "skipped")
        execs = tx.list_executions(run.id)
        ran = {x.node_id for x in execs}
        node = str(a.get("node") or "")
        if node not in changed:
            node = next((n for n in changed if n in ran), "")
        if not node:
            raise ProbeError("the edit changes no agent node that ran: nothing to replay",
                             "skipped")  # fmt: skip
        x = pick_execution(execs, node)
        if x is None:
            raise ProbeError(f"node {node} didn't run in run {run.id}", "skipped")
        links = [
            ln
            for ln in tx.list_artifact_links(run.id)
            if ln.execution_id == x.id and ln.direction == "read"
        ]
        inputs: dict[str, Any] = {"payload": run.payload}
        notes: list[str] = []
        for ln in links:
            inputs[ln.name] = app.scheduler.artifacts.load(tx, ln.hash)
        declared = [n.rstrip("?") for n in old.nodes[node].inputs if n.rstrip("?") != "payload"]
        if any(d not in inputs for d in declared):
            with contextlib.suppress(Exception):  # before lineage: the latest outputs
                got = app.scheduler.resolve_inputs(tx, x)
                for k, v in got.items():
                    inputs.setdefault(k, v)
                notes.append("inputs taken from the latest outputs (no lineage record)")
        output = None
        if x.result_ref:
            with contextlib.suppress(Exception):
                output = app.scheduler.artifacts.load(tx, x.result_ref)
        atts = (x.detail or {}).get("attachments") or {}
        diff = ""
        if "diff" in atts and atts["diff"].get("ref"):
            with contextlib.suppress(Exception):
                raw = app.scheduler.artifacts.read_bytes(tx, atts["diff"]["ref"]) or b""
                diff = raw.decode("utf-8", errors="replace")
        names = sorted(n for n in atts if n.startswith("transcript"))
        transcript = ""
        if names:
            with contextlib.suppress(Exception):
                raw = app.scheduler.artifacts.read_bytes(tx, atts[names[-1]]["ref"]) or b""
                transcript = raw.decode("utf-8", errors="replace")
        tasks = tx.list_tasks(run.id, x.id)
    context = {
        **inputs,
        "payload": run.payload,
        "run": {"id": run.id},
        "node": {"id": node, "iteration": x.iteration, "attempt": 1},
        "pipeline": {},
    }
    n = new.nodes[node]
    if n.for_each:
        first = min(tasks, key=lambda t: t.idx) if tasks else None
        item = first.item if first is not None else None
        if item is None:
            with contextlib.suppress(Exception):
                items = lookup(context, n.for_each)
                if items is not MISSING and isinstance(items, (list, dict)) and items:
                    item = (list(items.values()) if isinstance(items, dict) else items)[0]
        context.update(item=item, item_index=0)
        notes.append("for_each: replayed the first item only")
    ws = None
    if n.workspace is not None:
        ws = dict((x.detail or {}).get("workspace") or {})
        if isinstance(output, dict) and output.get("mirror"):
            ws.setdefault("mirror", output["mirror"])
        if not ws.get("start_sha") or not ws.get("repo"):
            raise ProbeError("the execution recorded no workspace commit to start from", "skipped")
        if n.workspace.type == "container":
            notes.append("container node replayed in a local clone (no container)")
    baseline = {
        "status": x.status,
        "output": output,
        "error": x.error,
        "diff": diff,
        "transcript": transcript,
    }
    return Target(run.id, run.flow, node, x, old.nodes[node], n, new.defaults, inputs, context,
                  ws, baseline, notes)  # fmt: skip


# -- the replay -------------------------------------------------------------------------------
class ProbeWorkspace:
    """A throwaway clone at a commit, without a remote. No declared secrets reach commands."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.host_path = path

    async def exec(
        self,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        input: str | None = None,
    ) -> tuple[int, str, str]:
        from agentline.runners.common import GIT_IDENTITY, base_env, run_process

        full = {**base_env(), **GIT_IDENTITY, **_strip_env(env or {})}
        try:
            return await run_process(cmd, cwd=self.path, env=full, input=input, timeout=timeout)
        except FileNotFoundError:
            return 127, "", f"{cmd[0]}: command not found\n"


def _strip_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if not k.upper().startswith(NO_ENV)}


def _mirror_for(ws: dict[str, Any]) -> Path:
    from agentline.workspaces import git

    if ws.get("mirror"):
        return Path(ws["mirror"])
    return git.agentline_home() / "mirrors" / f"{git.safe_name(str(ws['repo']))}.git"


async def make_clone(ws: dict[str, Any], dest: Path) -> ProbeWorkspace:
    from agentline.workspaces import git

    mirror = _mirror_for(ws)
    if not (mirror / "HEAD").exists():
        raise ProbeError(f"no local mirror of {ws['repo']} at {mirror}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await git.git("clone", "--quiet", "--no-checkout", str(mirror), str(dest))
        await git.git("remote", "remove", "origin", cwd=dest)
        await git.git("checkout", "--quiet", "--detach", str(ws["start_sha"]), cwd=dest)
    except git.GitError as e:
        raise ProbeError(f"couldn't check out {str(ws['start_sha'])[:12]}: {e}") from None
    return ProbeWorkspace(dest)


async def clone_diff(ws: ProbeWorkspace, start: str) -> str:
    from agentline.workspaces import git

    with contextlib.suppress(git.GitError):
        await git.git("add", "-A", ".", cwd=ws.path)
        return await git.git("diff", "--cached", start, cwd=ws.path)
    return ""


async def replay(files: Any, t: Target, budget_usd: float, probe_id: str) -> dict[str, Any]:
    """Run the edited node once. {output, error, cost_usd, diff, transcript, schema_errors}."""
    from agentline.mcp import resolved
    from agentline.pipeline import schema as jsonschema
    from agentline.pipeline.compile import agent_choice
    from agentline.pipeline.template import TemplateError, render, render_value
    from agentline.registry import Limits, RunRequest
    from agentline.runners.common import RunnerError
    from agentline.skills import SkillLibrary, install_into, prompt_section

    n = t.new
    cfg = files.config
    try:
        runner_name, base_params, _ = agent_choice(n, t.defaults, cfg)
        runner = files.registry.get_runner(runner_name)
        prompt = render(n.prompt or "", t.context)
        params = {**base_params, **render_value(n.params, t.context)}
    except (ValidationError, TemplateError) as e:
        raise ProbeError(f"can't prepare the node: {e}", "skipped") from None
    if isinstance(params.get("env"), list):
        params["env"] = [e for e in params["env"] if not str(e).upper().startswith(NO_ENV)]
    notes = []
    if n.mcp:
        known = cfg.mcp if cfg else {}
        safe = {m: known[m] for m in n.mcp if m in known and known[m].enabled
                and getattr(known[m], "probe_safe", False)}  # fmt: skip
        with contextlib.suppress(ValidationError):
            params["mcp_servers"] = {m: resolved(s) for m, s in safe.items()}
        if off := [m for m in n.mcp if m not in safe]:
            notes.append("MCP off in the probe: " + ", ".join(off))
    ws: ProbeWorkspace | None = None
    root = probes_dir() / probe_id
    out: dict[str, Any] = {"output": None, "error": None, "cost_usd": 0.0, "diff": "",
                           "transcript": "", "schema_errors": [], "notes": notes}  # fmt: skip
    try:
        if t.workspace is not None:
            ws = await make_clone(t.workspace, root / "repo")
        if n.skills:
            try:
                skills = SkillLibrary().resolve(list(n.skills))
            except ValidationError as e:
                raise ProbeError(str(e), "skipped") from None
            if runner_name == "claude-code" and ws is not None:
                install_into(ws.path, skills)
            else:
                prompt += prompt_section(skills)
        limits = Limits(
            allowed_tools=n.allowed_tools,
            max_turns=n.max_turns or t.defaults.max_turns,
            budget_usd=budget_usd,
            timeout=n.timeout,
        )
        request = RunRequest(prompt=prompt, inputs=t.inputs, limits=limits, workspace=ws,
                             params=params, run_id=f"probe-{t.run_id}", node_id=t.node,
                             iteration=t.execution.iteration)  # fmt: skip
        try:
            res = await runner.run(request)
        except RunnerError as e:
            out.update(error=str(e), cost_usd=float(e.cost_usd or 0.0),
                       transcript=e.transcript or "")  # fmt: skip
        else:
            out.update(output=res.output, cost_usd=float(res.cost_usd or 0.0),
                       transcript=res.transcript or "")  # fmt: skip
            if n.output_schema is not None:
                out["schema_errors"] = jsonschema.validate(res.output, n.output_schema)[:20]
        if ws is not None:
            out["diff"] = await clone_diff(ws, str(t.workspace["start_sha"]))  # type: ignore[index]
    finally:
        await asyncio.to_thread(shutil.rmtree, root, True)
    return out


# -- the comparison ---------------------------------------------------------------------------
def _show(v: Any, n: int = MAX_SHOW) -> str:
    text = v if isinstance(v, str) else json.dumps(v, indent=1, default=str)
    return text[:n] + (" …" if len(text) > n else "")


def _side(d: dict[str, Any]) -> str:
    parts = [f"status: {d.get('status') or ('failed' if d.get('error') else 'done')}"]
    if d.get("error"):
        parts.append(f"error: {str(d['error'])[:1500]}")
    parts.append("output:\n" + _show(d.get("output")))
    if d.get("schema_errors"):
        parts.append("output_schema errors: " + "; ".join(map(str, d["schema_errors"][:8])))
    if d.get("diff"):
        parts.append("code diff:\n" + _show(d["diff"], 8000))
    if d.get("transcript"):
        parts.append("transcript (end):\n" + str(d["transcript"])[-1500:])
    return "\n".join(parts)


def edit_text(t: Target) -> str:
    """What the edit changes in the node, as a diff (for the comparer and the UI)."""
    lines = []
    for k in COMPARED:
        a, b = getattr(t.old, k), getattr(t.new, k)
        if a == b:
            continue
        if k == "prompt":
            lines += difflib.unified_diff(
                (a or "").splitlines(), (b or "").splitlines(), "prompt (before)",
                "prompt (after)", lineterm="", n=2,
            )  # fmt: skip
        else:
            lines.append(f"{k}: {_show(a, 600)} -> {_show(b, 600)}")
    return "\n".join(lines)[:6000]


def compare_prompt(problem: str, a: str, b: str) -> str:
    return (
        f"{COMPARE_GUIDE}\n## PROBLEM\n{problem}\n\n## EXECUTION A\n{a}\n\n"
        f"## EXECUTION B\n{b}\n\nReply with the JSON object."
    )


def parse_compare(output: Any) -> tuple[str, str]:
    from agentline.assistant import _json_object

    data = output if isinstance(output, dict) else _json_object(str(output or ""))
    if not data:
        raise ValidationError("the comparer didn't answer with a JSON object")
    better = str(data.get("better") or "").strip().upper()
    if better not in ("A", "B", "SAME"):
        raise ValidationError(f"the comparer gave no answer A, B or same ({better or 'none'})")
    return better, str(data.get("reasoning") or "").strip()[:2000]


def candidate_first(probe_id: str) -> bool:
    """Whether the edited run is shown as A (stable per probe, so a redo shows the same)."""
    return int(hashlib.sha1(probe_id.encode()).hexdigest(), 16) % 2 == 0


# -- one probe --------------------------------------------------------------------------------
_inflight: set[str] = set()


def _summary(v: Any) -> str:
    return _show(v, 1500)


async def probe(
    app: Any,
    files: Any,
    review_id: str,
    index: int,
    *,
    store: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """Probe proposal `index` of a review; the result is kept in the review record under
    `probes[index]` and on the card (`probe`). Returns the probe."""
    from agentline import assistant, review
    from agentline.triage import _budget_left, _chat_harness

    store = store or review.ReviewStore()
    rec = store.get(review_id)
    if rec is None:
        raise KeyError(review_id)
    actions = rec.get("actions") or []
    if not 0 <= index < len(actions):
        raise KeyError(f"{review_id}#{index}")
    key = str(index)
    old = (rec.get("probes") or {}).get(key)
    if old and not force and old.get("status") in ("done", "running"):
        return dict(old)
    pid = "p" + hashlib.sha1(f"{review_id}:{index}".encode()).hexdigest()[:12]
    if pid in _inflight:
        return dict(old or {"id": pid, "status": "running"})
    _inflight.add(pid)
    p: dict[str, Any] = {"id": pid, "index": index, "status": "running", "at": time.time(),
                         "node": None, "iteration": None, "verdict": None, "reasoning": "",
                         "cost_usd": 0.0, "edit": "", "notes": [], "error": None,
                         "baseline": None, "candidate": None, "samples": 1}  # fmt: skip

    def save() -> None:
        cur = store.get(review_id) or rec
        cur.setdefault("probes", {})[key] = p
        spent = sum(float(x.get("cost_usd") or 0) for x in cur["probes"].values())
        cur["probe_cost_usd"] = round(spent, 6)
        acts = cur.get("actions") or []
        if index < len(acts):
            acts[index]["probe"] = {"status": p["status"], "verdict": p["verdict"],
                                    "reasoning": p["reasoning"][:400]}  # fmt: skip
        store.save(cur)

    try:
        save()
        cfg = getattr(files, "config", None)
        cap = review.config(cfg)["probe_budget_usd"] - float(rec.get("probe_cost_usd") or 0.0)
        if old:
            cap += float(old.get("cost_usd") or 0.0)
        if cap <= 0:
            raise ProbeError("this review's probe budget is used up ([review] probe_budget_usd)",
                             "skipped")  # fmt: skip
        ok, why = await asyncio.to_thread(_budget_left, app, rec["run"])
        if not ok:
            raise ProbeError(why.replace("triage", "a probe"), "skipped")
        t = await asyncio.to_thread(target_for, app, files, rec, actions[index])
        p.update(node=t.node, iteration=t.execution.iteration, edit=edit_text(t),
                 notes=list(t.notes))  # fmt: skip
        save()
        got = await replay(files, t, cap, pid)
        p["cost_usd"] = got["cost_usd"]
        p["notes"] += got.pop("notes", [])
        await asyncio.to_thread(review.charge, app, rec["run"], got["cost_usd"], "probe")
        base = {**t.baseline, "status": t.baseline["status"]}
        cand = {**got, "status": "failed" if got["error"] else "done"}
        p["baseline"] = {
            "status": base["status"],
            "output": _summary(base["output"]),
            "error": base.get("error"),
            "diff": str(base.get("diff") or "")[:4000],
        }
        p["candidate"] = {"status": cand["status"], "output": _summary(cand["output"]),
                          "error": cand.get("error"), "diff": str(cand.get("diff") or "")[:4000],
                          "schema_errors": cand["schema_errors"]}  # fmt: skip
        problem = "\n".join(
            f"- {r['node']} {r['component']}: {r['observed']}"
            for r in rec.get("rows") or []
            if r.get("node") == t.node
        ) or (rec.get("summary") or "")
        first = candidate_first(pid)
        a_text, b_text = (_side(cand), _side(base)) if first else (_side(base), _side(cand))
        harness, model = _chat_harness()
        try:
            output, cost = await assistant._ask(
                files, harness, model, compare_prompt(problem, a_text, b_text)
            )
        except assistant.AssistantError as e:
            raise ProbeError(f"the comparison couldn't run: {e}") from None
        p["cost_usd"] += float(cost or 0.0)
        await asyncio.to_thread(review.charge, app, rec["run"], float(cost or 0.0), "probe")
        try:
            better, reasoning = parse_compare(app.redactor.redact_value(output))
        except ValidationError as e:
            raise ProbeError(str(e)) from None
        if better == "SAME":
            p["verdict"] = "same"
        else:
            p["verdict"] = "better" if (better == "A") == first else "worse"
        p["reasoning"] = app.redactor.redact(reasoning)
        p["status"] = "done"
    except ProbeError as e:
        p.update(status=e.status, error=str(e))
    except Exception as e:  # a probe must never take the review loop down
        p.update(status="failed", error=f"{type(e).__name__}: {e}")
    finally:
        p["finished_at"] = time.time()
        with contextlib.suppress(Exception):
            save()
        _inflight.discard(pid)
    return dict(p)
