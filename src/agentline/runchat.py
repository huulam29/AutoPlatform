"""The chat start node (gatekeeper) and a run's conversation with it.

    start: ...                    # see agentline.pipeline.start
    nodes:
      intake:
        type: chat
        harness: claude-code
        prompt: prompts/intake.md    # what to collect, when the request is ready
        output_schema: schemas/request.json
        out: request

A run of such a pipeline starts with your first message (`payload.text`). The conversation is
the run's thread (`run_messages` in the ledger):

- **Intake** (the node's step): each turn the harness reads the thread and answers
  `ask` (a follow-up; the step parks with `ctx.wait(..., resume=True)` and the next message
  wakes it), `handoff` (its `output` becomes the node's artifact and the pipeline starts) or
  `cancel`.
- **During the run** (`RunChatLoop`, run by `serve`): the loop posts system notes for nodes
  that fail, gates that wait and the run's end, and the gatekeeper explains them; your
  messages get an answer from the same harness with the run's live state. What it proposes to
  do (approve / reject / retry / cancel) comes as cards that run only after a Confirm
  (`apply_card`), audited `via="run-chat"`.

Costs of turns outside the step go to the run (`add_cost(on_execution=False, touch=False)`).
AGENTLINE_RUNCHAT=off turns the loop off (tests and the e2e harness).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import asdict
from typing import Any

from agentline.errors import PermanentError, ValidationError
from agentline.ledger.base import RunMessage
from agentline.pipeline import schema as jsonschema
from agentline.pipeline.spec import NodeSpec, PipelineSpec

log = logging.getLogger("agentline.runchat")

INTAKE_ACTIONS = ("ask", "handoff", "cancel")
RUN_ACTIONS = ("approve", "reject", "retry", "cancel")
LOOP_INTERVAL = 1.0
NOTE_EXPLAIN = ("failed", "needs-review", "gate", "done", "dead")  # notes the agent explains
MAX_THREAD = 60  # messages the prompt carries (older ones are summarized by count)

GUIDE_INTAKE = """\
You are the gatekeeper of the pipeline {pipeline!r}: the person talks to you, you make sure
the request is clear and complete, then you hand it to the pipeline.

Each turn, answer with ONE JSON object and nothing else:
  {{"say": "<your message to the person>", "action": "ask" | "handoff" | "cancel",
   "output": <only with handoff: the request for the pipeline>}}
- ask: you need more information; `say` asks it (one or two short questions).
- handoff: the request is ready; `say` tells the person what happens next and `output` is what
  the pipeline gets{schema_note}.
- cancel: the person wants to stop, or the request can't be done; `say` says why.
Don't hand off until what the instructions below need is known."""

GUIDE_RUNNING = """\
You are the gatekeeper of the pipeline {pipeline!r}. You handed the request to the pipeline;
it is running now. Keep the person informed: explain failures, waiting approvals and the result
in plain words, answer their questions from the run state below, and propose actions.

Answer with ONE JSON object and nothing else:
  {{"say": "<your message>", "action": "none",
   "run_actions": [{{"type": "approve" | "reject" | "retry" | "cancel", "node": "<node id>",
                    "comment": "<why>"}}]}}
- run_actions are optional and only proposals: the person confirms each one before it runs.
  approve / reject need a waiting gate's node; retry needs a failed node (or no node: every
  failed one); cancel stops the whole run.
- Propose an action only when the person asked for it or it is clearly the next step.
- Never claim an action happened: it happens when the person confirms it."""


def chat_key(run_id: str, node_id: str) -> str:
    return f"run-chat:{run_id}:{node_id}"


def enabled() -> bool:
    return os.environ.get("AGENTLINE_RUNCHAT", "").strip().lower() not in ("off", "0", "false")


def chat_node(spec: PipelineSpec | None) -> NodeSpec | None:
    if spec is None:
        return None
    return next((n for n in spec.nodes.values() if n.type == "chat"), None)


def spec_of(app: Any, run: Any) -> PipelineSpec | None:
    try:
        return app.graph(run.flow, run).meta.get("spec")  # type: ignore[no-any-return]
    except Exception:
        return None


# -- the thread ---------------------------------------------------------------------------
def post(
    app: Any,
    run_id: str,
    role: str,
    text: str,
    *,
    via: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    text = app.redactor.redact(text)
    with app.ledger.tx() as tx:
        return int(tx.add_run_message(run_id, role, text, via=via, detail=detail))


def first_message(run: Any) -> RunMessage | None:
    """The message that started the run: its payload's `text` (or the JSON payload). It isn't
    stored; it is message 0 of the thread."""
    p = run.payload
    text = p.get("text") if isinstance(p, dict) else None
    if not isinstance(text, str) or not text.strip():
        text = json.dumps(p) if p else ""
    if not text.strip():
        return None
    return RunMessage(0, run.id, run.created_at, "user", text.strip(), via="start")


def thread(app: Any, run_id: str, after: int = 0, limit: int = 1000) -> list[RunMessage]:
    """The run's conversation, oldest first; `after=0` starts with the first message (id 0)."""
    with app.ledger.tx() as tx:
        stored = list(tx.list_run_messages(run_id, after, limit))
        run = tx.get_run(run_id) if after <= 0 else None
    first = first_message(run) if run is not None else None
    return ([first] if first else []) + stored


def message_dict(m: RunMessage) -> dict[str, Any]:
    return asdict(m)


async def say(app: Any, run_id: str, text: str, *, via: str = "app") -> dict[str, Any]:
    """A message from the person: into the thread, and it wakes the chat node if it waits."""
    run = app.get_run(run_id)
    if run is None:
        raise ValidationError(f"no run {run_id}")
    node = chat_node(spec_of(app, run))
    if node is None:
        raise ValidationError(f"run {run_id}'s pipeline has no chat start node")
    text = text.strip()
    if not text:
        raise ValidationError("the message is empty")
    mid = await asyncio.to_thread(post, app, run_id, "user", text, via=via)
    # the event wakes a waiting intake step; one that's running picks it up when it parks
    await app.deliver_event(chat_key(run_id, node.id), {"message": mid})
    return {"id": mid}


# -- run state for the prompt --------------------------------------------------------------
def _short(value: Any, cap: int = 600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= cap else text[:cap] + "…"


def live_text(app: Any, run: Any, spec: PipelineSpec | None) -> str:
    lines = [f"Run {run.id}: {run.status}"]
    if run.error:
        lines.append(f"Run error: {_short(run.error, 800)}")
    latest: dict[str, Any] = {}
    for x in app.executions(run.id):
        latest[x.node_id] = x  # executions come oldest first
    for node_id, x in latest.items():
        kind = spec.nodes[node_id].type if spec and node_id in spec.nodes else ""
        row = f"- {node_id} ({kind}, iteration {x.iteration}): {x.status}"
        if x.status == "waiting" and x.detail.get("kind") == "gate":
            row += f" — waiting for approval: {_short(x.detail.get('prompt', ''), 300)}"
        if x.error:
            row += f" — error: {_short(x.error, 500)}"
        if x.status == "done" and kind not in ("router", "gate", "chat"):
            with contextlib.suppress(Exception):
                row += f" — output: {_short(app.result(run.id, node_id), 400)}"
        lines.append(row)
    if spec is not None:
        pending = [n for n in spec.nodes if n not in latest]
        if pending:
            lines.append(f"Not started yet: {', '.join(pending)}")
    return "\n".join(lines)


def _thread_text(msgs: list[RunMessage]) -> str:
    who = {"user": "Person", "agent": "You", "system": "Pipeline"}
    shown = msgs[-MAX_THREAD:]
    head = (
        [f"({len(msgs) - len(shown)} earlier messages not shown)"] if len(msgs) > len(shown) else []
    )
    return "\n".join(head + [f"{who.get(m.role, m.role)}: {m.text}" for m in shown])


def build_prompt(
    n: NodeSpec,
    spec: PipelineSpec,
    instructions: str,
    msgs: list[RunMessage],
    *,
    phase: str,
    live: str = "",
) -> str:
    if phase == "intake":
        note = ""
        if n.output_schema is not None:
            note = " and must match this JSON schema:\n" + json.dumps(n.output_schema, indent=1)
        guide = GUIDE_INTAKE.format(pipeline=spec.name, schema_note=note)
    else:
        guide = GUIDE_RUNNING.format(pipeline=spec.name)
    parts = [guide, "## Instructions\n" + instructions.strip()]
    if live:
        parts.append("## Run state\n" + live)
    parts.append("## Conversation\n" + _thread_text(msgs))
    return "\n\n".join(parts) + "\n"


def check_reply(out: Any, n: NodeSpec, phase: str) -> tuple[dict[str, Any] | None, list[str]]:
    if isinstance(out, str):
        from agentline.runners.common import parse_json_text

        ok, parsed = parse_json_text(out)
        out = parsed if ok else out
    if not isinstance(out, dict):
        return None, ["answer with a single JSON object"]
    errors: list[str] = []
    text = out.get("say")
    if not isinstance(text, str) or not text.strip():
        errors.append("'say' must be a non-empty string")
    action = out.get("action", "ask" if phase == "intake" else "none")
    allowed = INTAKE_ACTIONS if phase == "intake" else ("none",)
    if action not in allowed:
        errors.append(f"'action' must be one of {', '.join(allowed)}")
    reply: dict[str, Any] = {"say": str(text or "").strip(), "action": action}
    if phase == "intake" and action == "handoff":
        output = out.get("output")
        if n.output_schema is not None:
            errors += [f"output: {e}" for e in jsonschema.validate(output, n.output_schema)][:10]
        elif output is None:
            output = {"request": reply["say"]}
        reply["output"] = output
    if phase == "running":
        cards = []
        raw = out.get("run_actions") or []
        if not isinstance(raw, list):
            errors.append("'run_actions' must be a list")
            raw = []
        for i, a in enumerate(raw):
            if not isinstance(a, dict) or a.get("type") not in RUN_ACTIONS:
                errors.append(f"run_actions[{i}].type must be one of {', '.join(RUN_ACTIONS)}")
                continue
            card = {"type": a["type"], "state": "proposed"}
            if isinstance(a.get("node"), str) and a["node"]:
                card["node"] = a["node"]
            if isinstance(a.get("comment"), str):
                card["comment"] = a["comment"][:500]
            cards.append(card)
        reply["run_actions"] = cards
    return (None, errors) if errors else (reply, [])


# -- one harness turn ------------------------------------------------------------------------
async def call_harness(
    n: NodeSpec,
    spec: PipelineSpec,
    prompt: str,
    *,
    phase: str,
    registry: Any,
    config: Any,
    run_id: str,
    iteration: int = 1,
    budget_usd: float | None = None,
    spend: Any = None,
    attach: Any = None,
) -> dict[str, Any]:
    """Run the node's harness (on the host, no workspace) and return a checked reply; a reply
    that fails the checks gets one repair. `spend(usd)` / `attach(label, transcript)` record
    costs and transcripts."""
    from agentline.pipeline.compile import REPAIR_PROMPT, agent_choice
    from agentline.registry import Limits, RunRequest
    from agentline.runners.common import RunnerError

    try:
        runner_name, params, _notes = agent_choice(n, spec.defaults, config)
    except ValidationError as e:
        raise PermanentError(str(e)) from None
    runner = registry.get_runner(runner_name)
    if n.mcp:
        from agentline.pipeline.compile import _mcp_servers

        params["mcp_servers"] = _mcp_servers(n, config)
    max_turns = n.max_turns or spec.defaults.max_turns
    errors: list[str] = []
    for repair in (False, True):
        label = f"{phase}.{'repair' if repair else 'first'}"
        try:
            res = await runner.run(
                RunRequest(
                    prompt=prompt,
                    inputs={},
                    limits=Limits(
                        allowed_tools=n.allowed_tools,
                        max_turns=max_turns,
                        budget_usd=budget_usd,
                        timeout=n.timeout,
                    ),
                    workspace=None,
                    params=dict(params),
                    run_id=run_id,
                    node_id=n.id,
                    iteration=iteration,
                )
            )
        except RunnerError as e:
            if e.cost_usd and spend is not None:
                await spend(e.cost_usd)
            raise
        if res.cost_usd and spend is not None:
            await spend(res.cost_usd)
        if res.transcript and attach is not None:
            await attach(label, res.transcript)
        reply, errors = check_reply(res.output, n, phase)
        if reply is not None:
            return reply
        prompt += REPAIR_PROMPT.format(errors="\n".join(f"- {e}" for e in errors))
    raise RuntimeError(f"the gatekeeper's answer is still not usable: {'; '.join(errors[:5])}")


def instructions_for(n: NodeSpec, payload: Any, run_id: str) -> str:
    from agentline.pipeline.template import TemplateError, render

    try:
        return render(n.prompt or "", {"payload": payload, "run": {"id": run_id}})
    except TemplateError:
        return n.prompt or ""


# -- the node's step (intake) ----------------------------------------------------------------
async def intake_step(ctx: Any, n: NodeSpec, spec: PipelineSpec, registry: Any, config: Any) -> Any:
    app = ctx.app
    run_id = ctx.run_id
    msgs = await asyncio.to_thread(thread, app, run_id)
    key = chat_key(run_id, n.id)
    if not msgs or msgs[-1].role != "user":
        # woken with nothing new to answer (or no first message yet): keep waiting
        return ctx.wait(key, resume=True, kind="chat")
    prompt = build_prompt(n, spec, instructions_for(n, ctx.payload, run_id), msgs, phase="intake")
    await ctx.check_budget(0.0)
    if n.skills:
        from agentline.pipeline.compile import _load_skills

        prompt = _load_skills(ctx, n, "", None, prompt)
    turn = sum(1 for m in msgs if m.role == "agent") + 1

    async def spend(usd: float) -> None:
        await ctx.spend(usd, f"chat:{n.id}")

    async def attach(label: str, transcript: str) -> None:
        await ctx.attach(f"transcript.turn{turn}.{label}", transcript)

    reply = await call_harness(
        n,
        spec,
        prompt,
        phase="intake",
        registry=registry,
        config=config,
        run_id=run_id,
        iteration=ctx.iteration,
        budget_usd=await ctx.remaining_budget(),
        spend=spend,
        attach=attach,
    )
    detail = {"action": reply["action"], "turn": turn}
    await asyncio.to_thread(post, app, run_id, "agent", reply["say"], via=n.id, detail=detail)
    ctx.notes["turns"] = turn
    if reply["action"] == "handoff":
        return reply["output"]
    if reply["action"] == "cancel":  # RunChatLoop cancels the run (not from inside its own step)
        return ctx.wait(key, resume=True, kind="chat", closing=True)
    return ctx.wait(key, resume=True, kind="chat")


# -- cards (run actions) -----------------------------------------------------------------
def guards_deploy(app: Any, registry: Any, flow: str, node_id: str) -> bool:
    """Approving this gate lets a deploy-class action run (ask twice)."""
    try:
        spec = app.graph(flow).meta.get("spec")
    except Exception:
        return True  # unknown pipeline: ask twice to be safe
    if spec is None:
        return True
    for t in [e.dst for e in spec.edges if e.src == node_id]:
        n = spec.nodes.get(t)
        if n is None or n.type != "action":
            continue
        action = registry.actions.get(n.uses or "") if registry is not None else None
        if action is None or getattr(action, "deploy_class", False):
            return True
    return False


async def apply_card(
    app: Any,
    run_id: str,
    message_id: int,
    index: int,
    *,
    actor: str | None = None,
    via: str = "run-chat",
    dismiss: bool = False,
) -> dict[str, Any]:
    """Confirm (or dismiss) one proposed action of an agent message."""
    msgs = await asyncio.to_thread(thread, app, run_id)
    msg = next((m for m in msgs if m.id == message_id), None)
    if msg is None:
        raise ValidationError(f"no message {message_id} in run {run_id}")
    cards = list(msg.detail.get("actions") or [])
    if not 0 <= index < len(cards):
        raise ValidationError(f"message {message_id} has no action {index}")
    card = dict(cards[index])
    if card.get("state") != "proposed":
        raise ValidationError(f"that action was already {card.get('state')}")
    if dismiss:
        card["state"] = "dismissed"
    else:
        kind, node = card["type"], card.get("node")
        try:
            if kind == "approve":
                await app.approve(
                    run_id, node, actor=actor, comment=card.get("comment", ""), via=via
                )
            elif kind == "reject":
                await app.reject(
                    run_id, node, actor=actor, comment=card.get("comment", ""), via=via
                )
            elif kind == "retry":
                await app.retry(run_id, node_id=node, actor=actor, via=via)
            else:
                await app.cancel(run_id, actor=actor)
        except (ValidationError, PermissionError) as e:
            card["state"], card["error"] = "failed", str(e)
        else:
            card["state"] = "done"
    card["by"], card["at"] = actor, time.time()
    cards[index] = card
    detail = {**msg.detail, "actions": cards}

    def save() -> None:
        with app.ledger.tx() as tx:
            tx.update_run_message(message_id, detail)

    await asyncio.to_thread(save)
    if card["state"] in ("done", "failed"):
        what = card["type"] + (f" {card['node']}" if card.get("node") else "")
        text = f"{what}: done" if card["state"] == "done" else f"{what} failed: {card['error']}"
        await asyncio.to_thread(
            post,
            app,
            run_id,
            "system",
            text,
            via=via,
            detail={"event": f"card:{message_id}:{index}"},
        )
    return card


# -- the loop during the run -----------------------------------------------------------------
def _notes_due(
    app: Any, run: Any, spec: PipelineSpec, n: NodeSpec, posted: set[str]
) -> list[tuple[str, str, str]]:
    """(event key, kind, text) for what happened and isn't in the thread yet."""
    out: list[tuple[str, str, str]] = []
    for x in app.executions(run.id):
        if x.node_id == n.id:
            continue
        if x.status == "dead":
            key = f"dead:{x.id}:{x.detail.get('retries', 0)}:{x.attempt}"
            text = f"{x.node_id} failed: {_short(x.error or 'no error message', 800)}"
            out.append((key, "failed", text))
        elif x.status == "needs-review":
            key = f"review:{x.id}:{x.detail.get('retries', 0)}:{x.attempt}"
            text = f"{x.node_id} needs review ({x.reason or 'see the run'})"
            out.append((key, "needs-review", text))
        elif x.status == "waiting" and x.detail.get("kind") == "gate":
            key = f"gate:{x.id}"
            text = f"{x.node_id} waits for approval: {_short(x.detail.get('prompt', ''), 400)}"
            out.append((key, "gate", text))
    if run.status in ("done", "dead", "cancelled"):
        key = f"run:{run.status}:{run.updated_at}"
        out.append((key, run.status, f"The run is {run.status}."))
    return [o for o in out if o[0] not in posted]


class RunChatLoop:
    """Notes and answers for runs whose intake is over (see the module docstring)."""

    def __init__(self, app: Any, files: Any) -> None:
        self.app = app
        self.files = files  # .registry / .config follow live reloads
        self.busy: set[str] = set()  # runs with a turn in flight (the API shows "thinking")

    def candidates(self) -> list[Any]:
        statuses = ("running", "waiting", "failed", "needs-review", "dead", "done", "cancelled")
        cutoff = time.time() - 2 * 24 * 3600
        out = []
        with self.app.ledger.tx() as tx:
            for s in statuses:
                for r in tx.list_runs(s, 200):
                    if r.updated_at >= cutoff or s not in ("done", "dead", "cancelled"):
                        out.append(r)
        return out

    async def step(self) -> None:
        for run in await asyncio.to_thread(self.candidates):
            spec = spec_of(self.app, run)
            n = chat_node(spec)
            if spec is None or n is None:
                continue
            try:
                await self.one(run, spec, n)
            except Exception as e:  # one run's trouble never stops the loop
                log.warning("run chat %s: %s", run.id, e)

    async def one(self, run: Any, spec: PipelineSpec, n: NodeSpec) -> None:
        app = self.app
        execs = await asyncio.to_thread(app.executions, run.id)
        mine = [x for x in execs if x.node_id == n.id]
        if not mine:
            return
        last = mine[-1]
        if last.status == "waiting" and last.detail.get("closing"):
            if run.status != "cancelled":
                await app.cancel(run.id, actor=f"gatekeeper:{n.id}")
            return
        if last.status != "done":
            return  # intake is still going (the step answers), or it failed
        msgs = await asyncio.to_thread(thread, app, run.id)
        posted = {str(m.detail.get("event")) for m in msgs if m.detail.get("event")}
        for key, kind, text in await asyncio.to_thread(_notes_due, app, run, spec, n, posted):
            await asyncio.to_thread(
                post,
                app,
                run.id,
                "system",
                text,
                via="pipeline",
                detail={"event": key, "kind": kind},
            )
        msgs = await asyncio.to_thread(thread, app, run.id)
        if not self.needs_answer(msgs):
            return
        self.busy.add(run.id)
        try:
            await self.answer(run, spec, n, msgs, last)
        finally:
            self.busy.discard(run.id)

    @staticmethod
    def needs_answer(msgs: list[RunMessage]) -> bool:
        """A message from the person, or a note worth explaining, after the agent's last line."""
        for m in reversed(msgs):
            if m.role == "agent":
                return False
            if m.role == "user" or (m.role == "system" and m.detail.get("kind") in NOTE_EXPLAIN):
                return True
        return False

    async def answer(
        self, run: Any, spec: PipelineSpec, n: NodeSpec, msgs: list[RunMessage], chat_ex: Any
    ) -> None:
        app = self.app
        run = app.get_run(run.id) or run
        live = await asyncio.to_thread(live_text, app, run, spec)
        prompt = build_prompt(
            n, spec, instructions_for(n, run.payload, run.id), msgs, phase="running", live=live
        )

        async def spend(usd: float) -> None:
            def rec() -> None:
                with app.ledger.tx() as tx:
                    tx.add_cost(
                        run.id,
                        chat_ex.id,
                        chat_ex.attempt,
                        usd,
                        f"chat:{n.id}",
                        touch=False,
                        on_execution=False,
                    )

            await asyncio.to_thread(rec)

        try:
            reply = await call_harness(
                n,
                spec,
                prompt,
                phase="running",
                registry=self.files.registry,
                config=self.files.config,
                run_id=run.id,
                spend=spend,
            )
        except Exception as e:
            await asyncio.to_thread(
                post,
                app,
                run.id,
                "agent",
                f"(I couldn't answer: {_short(str(e), 300)})",
                via=n.id,
                detail={"error": True},
            )
            return
        cards = reply.get("run_actions") or []
        for c in cards:
            if c["type"] == "approve" and c.get("node"):
                c["deploy"] = guards_deploy(app, self.files.registry, run.flow, c["node"])
        detail: dict[str, Any] = {"action": "none"}
        if cards:
            detail["actions"] = cards
        await asyncio.to_thread(post, app, run.id, "agent", reply["say"], via=n.id, detail=detail)

    async def run(self, stop: asyncio.Event, interval: float = LOOP_INTERVAL) -> None:
        while not stop.is_set():
            await self.step()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)


def thinking(app: Any, run_id: str, loop: RunChatLoop | None = None) -> bool:
    """Whether the gatekeeper is working on an answer (for the "thinking…" line)."""
    if loop is not None and run_id in loop.busy:
        return True
    run = app.get_run(run_id)
    node = chat_node(spec_of(app, run)) if run else None
    if node is None:
        return False
    mine = [x for x in app.executions(run_id) if x.node_id == node.id]
    if mine and mine[-1].status in ("ready", "running", "pending"):
        return True
    if loop is not None and mine and mine[-1].status == "done" and run.status not in ("cancelled",):
        return RunChatLoop.needs_answer(thread(app, run_id))
    return False
