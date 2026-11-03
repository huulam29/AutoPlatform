"""Compile a validated PipelineSpec into a Graph: one generic handler per node type."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from agentline.agents import choose, resolve
from agentline.config import Config
from agentline.context import StepContext
from agentline.errors import NeedsReview, PermanentError, ValidationError
from agentline.graph import DEFAULT_WHEN, EdgeDef, Graph, Handler, NodeDef
from agentline.pipeline import schema as jsonschema
from agentline.pipeline.spec import DefaultsSpec, NodeSpec, PipelineSpec
from agentline.pipeline.template import MISSING, TemplateError, lookup, render, render_value
from agentline.registry import (
    ActionCall,
    EffectPolicy,
    Limits,
    Registry,
    RunRequest,
    Workspace,
)
from agentline.results import Route, Wait
from agentline.runners.common import RunnerError
from agentline.workspaces import finalize_output, image_proposal

REPAIR_PROMPT = """

---
Your previous answer did not match the required output schema. Errors:
{errors}

Answer again with output that matches the schema exactly."""


def template_context(
    ctx: StepContext, inputs: dict[str, Any], pipeline: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Inputs plus the built-ins: payload, run, node and pipeline (`{{pipeline.dir}}`: the
    pipeline file's folder, for scripts kept next to it)."""
    return {
        **inputs,
        "payload": ctx.payload,
        "run": {"id": ctx.run_id},
        "node": {"id": ctx.node_id, "iteration": ctx.iteration, "attempt": ctx.attempt},
        "pipeline": pipeline or {},
    }


def _render(text: str, context: dict[str, Any]) -> str:
    try:
        return render(text, context)
    except TemplateError as e:
        raise PermanentError(str(e)) from None


def _render_value(value: Any, context: dict[str, Any]) -> Any:
    try:
        return render_value(value, context)
    except TemplateError as e:
        raise PermanentError(str(e)) from None


@contextlib.asynccontextmanager
async def _workspace(
    ctx: StepContext, n: NodeSpec, registry: Registry
) -> AsyncIterator[Workspace | None]:
    if n.workspace is None:
        yield None
        return
    provider = registry.get_workspace(n.workspace.type)
    async with provider.acquire(ctx, n.workspace) as ws:
        yield ws


def agent_choice(
    n: NodeSpec, defaults: DefaultsSpec, config: Config | None
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """(runner name, runner params from the agent/model choice, notes). A node's own `runner:`
    is the low-level escape hatch; otherwise `harness:`/`model:` (or the pipeline defaults)
    pick a configured harness and model (see agentline.agents)."""
    if n.runner:
        params = {"model": n.model} if n.model else {}
        return n.runner, params, {"runner": n.runner}
    res = resolve(*choose(n.harness, n.model, defaults.harness, defaults.model), config)
    notes: dict[str, Any] = {"runner": res.runner, "harness": res.harness.name}
    if res.model is not None:
        notes["model"] = {
            "name": res.model.name,
            "id": res.model.model,
            "provider": res.model.provider,
        }
    return res.runner, res.params, notes


TaskRun = Callable[[StepContext, dict[str, Any], str], Awaitable[Any]]


async def fan_out(
    ctx: StepContext, n: NodeSpec, context: dict[str, Any], run: TaskRun
) -> list[Any]:
    """A for_each node: one task per item, `max_parallel` at a time. Every task is recorded
    (tasks table + journal); a retry of the node reuses the tasks that finished and reruns only
    the others. Fails (and retries like any step) when any task failed."""
    import copy
    import time

    from agentline.ledger.base import TaskRecord

    try:
        items = lookup(context, n.for_each or "")
    except TemplateError as e:
        raise PermanentError(f"for_each: {e}") from None
    if items is MISSING or items is None:
        items = []
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list):
        raise PermanentError(f"for_each {n.for_each!r} is a {type(items).__name__}, not a list")
    ex = ctx.execution
    app = ctx.app
    clean = app.redactor.redact_value  # task records hold items, results and errors

    def db(fn: Callable[[Any], Any]) -> Any:
        with app.ledger.tx() as tx:
            return fn(tx)

    old = {
        t.idx: t for t in await asyncio.to_thread(db, lambda tx: tx.list_tasks(ex.run_id, ex.id))
    }
    results: list[Any] = [None] * len(items)
    failed: list[tuple[int, str]] = []
    sem = asyncio.Semaphore(max(1, n.max_parallel))

    def journal(tx: Any, idx: int, message: str, detail: dict[str, Any] | None = None) -> None:
        tx.add_journal(ex.run_id, "task", message, execution_id=ex.id, node_id=ex.node_id,
                       iteration=ex.iteration, task=idx, detail=detail)  # fmt: skip

    async def task(idx: int, item: Any) -> None:
        prev = old.get(idx)
        if prev is not None and prev.status == "done":
            results[idx] = prev.result
            return
        async with sem:
            rec = TaskRecord(ex.id, idx, ex.run_id, ex.node_id, ex.iteration, "running",
                             attempts=(prev.attempts if prev else 0) + 1, item=clean(item),
                             started_at=time.time())  # fmt: skip

            def start(tx: Any) -> None:
                tx.put_task(rec)
                journal(tx, idx, f"task {idx} started (attempt {rec.attempts})")

            await asyncio.to_thread(db, start)
            tctx = copy.copy(ctx)
            tctx.effect_prefix = f"task{idx}.{rec.attempts}:"  # a rerun really reruns
            try:
                out = await run(tctx, {"item": item, "item_index": idx}, f".task{idx}")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # recorded; the node fails after all tasks ran
                rec.status, rec.error, rec.finished_at = (
                    "failed",
                    clean(f"{type(e).__name__}: {e}")[:2000],
                    time.time(),
                )

                def fail(tx: Any) -> None:
                    tx.put_task(rec)
                    journal(tx, idx, f"task {idx} failed", {"error": rec.error})

                await asyncio.to_thread(db, fail)
                failed.append((idx, rec.error))
                return
            out = clean(out)  # successors see the redacted value anyway
            rec.status, rec.result, rec.finished_at = "done", out, time.time()

            def done(tx: Any) -> None:
                tx.put_task(rec)
                journal(tx, idx, f"task {idx} done")

            await asyncio.to_thread(db, done)
            results[idx] = out

    await asyncio.gather(*(task(i, item) for i, item in enumerate(items)))
    ctx.notes["tasks"] = {"total": len(items), "failed": len(failed)}
    if failed:
        first = "; ".join(f"task {i}: {err[:200]}" for i, err in sorted(failed)[:3])
        raise RuntimeError(f"{len(failed)} of {len(items)} tasks failed ({first})")
    return results


def _mcp_servers(n: NodeSpec, config: Config | None) -> dict[str, Any]:
    """The node's MCP servers with their ${KEY}s filled in (a missing key fails the node)."""
    from agentline.mcp import resolved

    known = config.mcp if config else {}
    try:  # a disabled server (enabled = false) is left out
        return {name: resolved(known[name]) for name in n.mcp if known[name].enabled}
    except KeyError as e:
        raise PermanentError(f"unknown MCP server {e.args[0]!r}") from None
    except ValidationError as e:
        raise PermanentError(str(e)) from None


def _load_skills(ctx: StepContext, n: NodeSpec, runner_name: str, ws: Any, prompt: str) -> str:
    """Claude Code with a workspace finds skills in `.claude/skills/`; everyone else gets them
    in the prompt. A missing skill fails the node (a config problem, not a retry)."""
    from agentline.skills import SkillLibrary, install_into, prompt_section

    try:
        skills = SkillLibrary().resolve(list(n.skills))
    except ValidationError as e:
        raise PermanentError(str(e)) from None
    ctx.notes["skills"] = [s.name for s in skills]
    if runner_name == "claude-code" and ws is not None:
        install_into(Path(getattr(ws, "host_path", ws.path)), skills)
        return prompt
    return prompt + prompt_section(skills)


def agent_handler(
    n: NodeSpec,
    registry: Registry,
    config: Config | None = None,
    defaults: DefaultsSpec | None = None,
    text_input: bool = False,
) -> Handler:
    """`text_input`: the start node of a text-input pipeline whose prompt doesn't read
    `{{payload.text}}` gets the text as an `## Input` section."""
    assert n.prompt is not None
    defaults = defaults or DefaultsSpec()
    runner_name, base_params, choice_notes = agent_choice(n, defaults, config)
    max_turns = n.max_turns or defaults.max_turns

    async def handler(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        context = template_context(ctx, inputs)
        if n.for_each:
            return await fan_out(
                ctx,
                n,
                context,
                lambda tctx, extra, sfx: one(tctx, inputs, {**context, **extra}, sfx),
            )
        return await one(ctx, inputs, context, "")

    async def one(
        ctx: StepContext, inputs: dict[str, Any], context: dict[str, Any], sfx: str
    ) -> Any:
        runner = registry.get_runner(runner_name)
        prompt = _render(n.prompt or "", context)
        if (
            text_input
            and isinstance(ctx.payload, dict)
            and isinstance(ctx.payload.get("text"), str)
        ):
            from agentline.pipeline.start import TEXT_SECTION

            prompt += TEXT_SECTION.format(text=ctx.payload["text"])
        params = {**base_params, **_render_value(n.params, context)}
        ctx.notes.update(choice_notes)
        if n.mcp:
            params["mcp_servers"] = _mcp_servers(n, config)
            ctx.notes["mcp"] = list(params["mcp_servers"])
            if off := [m for m in n.mcp if m not in params["mcp_servers"]]:
                ctx.notes["mcp_disabled"] = off
        async with _workspace(ctx, n, registry) as ws:
            if n.skills:
                prompt = _load_skills(ctx, n, runner_name, ws, prompt)
            for repair in (False, True):
                await ctx.check_budget(0.0)
                limits = Limits(
                    allowed_tools=n.allowed_tools,
                    max_turns=max_turns,
                    budget_usd=await ctx.remaining_budget(),
                    timeout=n.timeout,
                )
                label = ("repair" if repair else "first") + sfx
                try:
                    res = await runner.run(
                        RunRequest(
                            prompt=prompt,
                            inputs=inputs,
                            limits=limits,
                            workspace=ws,
                            params=params,
                            run_id=ctx.run_id,
                            node_id=ctx.node_id,
                            iteration=ctx.iteration,
                        )
                    )
                except RunnerError as e:  # a failed call can still have cost money
                    if e.transcript:
                        await ctx.attach(f"transcript.{label}", e.transcript)
                    if e.meta:
                        ctx.notes[f"runner_meta.{label}"] = e.meta
                    if e.cost_usd:
                        await ctx.spend(e.cost_usd, f"runner:{n.runner}")
                    raise
                if res.transcript:
                    await ctx.attach(f"transcript.{label}", res.transcript)
                if res.meta:
                    ctx.notes[f"runner_meta.{label}"] = res.meta
                if res.cost_usd:
                    await ctx.spend(res.cost_usd, f"runner:{n.runner}")
                if res.missing_tools:
                    raise NeedsReview(
                        "missing_tools",
                        {
                            "missing_tools": res.missing_tools,
                            "runner": n.runner,
                            "workspace": n.workspace.__dict__ if n.workspace else None,
                            "image_proposal": image_proposal(ws, res.missing_tools),
                        },
                    )
                if n.output_schema is None:
                    return await finalize_output(ws, res.output)
                errors = jsonschema.validate(res.output, n.output_schema)
                if not errors:
                    return await finalize_output(ws, res.output)
                ctx.notes[f"schema_errors.{label}"] = errors[:20]
                if repair:
                    raise RuntimeError(
                        f"output still fails its schema after a repair attempt: {errors[:5]}"
                    )
                prompt = prompt + REPAIR_PROMPT.format(
                    errors="\n".join(f"- {e}" for e in errors[:20])
                )
        raise AssertionError("unreachable")

    return handler


def action_handler(
    n: NodeSpec,
    registry: Registry,
    config: Config | None,
    pipeline: dict[str, Any] | None = None,
) -> Handler:
    async def handler(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        context = template_context(ctx, inputs, pipeline)
        if n.for_each:
            return await fan_out(
                ctx, n, context, lambda tctx, extra, _sfx: one(tctx, inputs, {**context, **extra})
            )
        return await one(ctx, inputs, context)

    async def one(ctx: StepContext, inputs: dict[str, Any], context: dict[str, Any]) -> Any:
        action = registry.get_action(n.uses or "")
        params = _render_value(n.params, context)
        key = _render(n.effect.key, context) if n.effect.key else None
        call = ActionCall(
            params=params,
            inputs=inputs,
            effect=EffectPolicy(key=key, on_unknown=n.effect.on_unknown),
            config=config,
        )
        result = await action(ctx, call)
        return {} if result is None else result

    return handler


LLM_ROUTER_PROMPT = """

---
Decide which branch to take. Answer with exactly one of these labels and nothing else:
{labels}"""

LLM_ROUTER_REPAIR = """

Your answer {answer!r} is not one of the labels. Answer with exactly one of: {labels}"""


def _pick_label(output: Any, labels: list[str]) -> str | None:
    if isinstance(output, dict):
        output = output.get("route") or output.get("label") or output.get("answer") or ""
    text = str(output).strip().strip("`'\".").strip()
    by_lower = {x.lower(): x for x in labels}
    if text.lower() in by_lower:
        return by_lower[text.lower()]
    found = [
        x for x in labels if re.search(rf"(?<![\w-]){re.escape(x)}(?![\w-])", str(output), re.I)
    ]
    return found[0] if len(found) == 1 else None


def llm_router_handler(
    n: NodeSpec,
    registry: Registry,
    labels: list[str],
    config: Config | None,
    defaults: DefaultsSpec,
) -> Handler:
    """An LLM reads the prompt and picks one of the router's edge labels. An answer that
    matches none gets one repair attempt; then the `default` edge if there is one."""
    res = resolve("builtin", n.model or defaults.model, config)
    choices = [x for x in labels if x != DEFAULT_WHEN]
    has_default = DEFAULT_WHEN in labels
    assert res.model is not None

    async def handler(ctx: StepContext, inputs: dict[str, Any]) -> Route:
        runner = registry.get_runner(res.runner)
        prompt = _render(n.prompt or "", template_context(ctx, inputs))
        prompt += LLM_ROUTER_PROMPT.format(labels="\n".join(f"- {x}" for x in choices))
        ctx.notes.update(
            runner=res.runner,
            harness="builtin",
            model={"name": res.model.name, "id": res.model.model, "provider": res.model.provider},
        )
        answer: Any = None
        for repair in (False, True):
            await ctx.check_budget(0.0)
            out = await runner.run(
                RunRequest(
                    prompt=prompt,
                    inputs=inputs,
                    limits=Limits(
                        allowed_tools=(), budget_usd=await ctx.remaining_budget(), timeout=n.timeout
                    ),
                    params=dict(res.params),
                    run_id=ctx.run_id,
                    node_id=ctx.node_id,
                    iteration=ctx.iteration,
                )
            )
            label_name = "repair" if repair else "first"
            if out.transcript:
                await ctx.attach(f"transcript.{label_name}", out.transcript)
            if out.cost_usd:
                await ctx.spend(out.cost_usd, f"router:{res.model.name}")
            answer = out.output
            if (label := _pick_label(answer, choices)) is not None:
                return Route(label, {"mode": "llm", "answer": answer})
            prompt += LLM_ROUTER_REPAIR.format(answer=str(answer)[:200], labels=", ".join(choices))
        if has_default:
            return Route(DEFAULT_WHEN, {"mode": "llm", "answer": answer, "unmatched": True})
        raise RuntimeError(
            f"router {n.id!r}: the model answered {str(answer)[:200]!r}, not a label"
        )

    return handler


def router_handler(n: NodeSpec) -> Handler:
    path = n.switch or ""

    async def handler(ctx: StepContext, inputs: dict[str, Any]) -> Route:
        value = lookup(inputs, path)
        if value is MISSING:
            raise PermanentError(f"router {n.id!r}: {path!r} is missing from its input")
        return Route(
            str(value).lower() if isinstance(value, bool) else str(value),
            {"switch": path, "value": value},
        )

    return handler


def chat_handler(
    n: NodeSpec, spec: PipelineSpec, registry: Registry, config: Config | None
) -> Handler:
    """The chat start node's intake: one harness turn per message (agentline.runchat)."""

    async def handler(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        from agentline.runchat import intake_step

        return await intake_step(ctx, n, spec, registry, config)

    return handler


def gate_handler(n: NodeSpec) -> Handler:
    async def handler(ctx: StepContext, inputs: dict[str, Any]) -> Wait:
        prompt = _render(n.prompt or "", template_context(ctx, inputs))
        return ctx.wait(
            f"gate:{ctx.execution.id}", kind="gate", prompt=prompt, approvers=list(n.approvers)
        )

    return handler


def wait_handler(n: NodeSpec, registry: Registry) -> Handler:
    async def handler(ctx: StepContext, inputs: dict[str, Any]) -> Wait:
        waiter = registry.get_waiter(n.wait_for or "")
        params = _render_value(n.params, template_context(ctx, inputs))
        # waiters that need a key unique to this wait (chat.ask) get the run's coordinates
        where = {"_run": ctx.run_id, "_node": ctx.node_id, "_iteration": ctx.iteration}
        key = waiter.correlation_key({**params, **where}, inputs)
        return ctx.wait(key, kind="wait", waiter=n.wait_for, params=params, poll_every=n.poll_every)

    return handler


def compile_pipeline(spec: PipelineSpec, registry: Registry, config: Config | None = None) -> Graph:
    g = Graph(spec.name, spec.budget_usd)
    g.meta["spec"] = spec
    pipe = {"name": spec.name, "dir": str(Path(spec.base_dir).resolve())}
    from agentline.pipeline.start import input_kind, start_node

    text_start = start_node(spec) if input_kind(spec) == "text" else None
    for n in spec.nodes.values():
        if n.type == "agent":
            reads_text = "payload.text" in (n.prompt or "")
            text_input = n.id == text_start and not reads_text
            handler = agent_handler(n, registry, config, spec.defaults, text_input)
            kind, timeout = "step", None  # the runner enforces the timeout
        elif n.type == "chat":
            handler, kind, timeout = chat_handler(n, spec, registry, config), "step", None
        elif n.type == "action":
            handler, kind, timeout = action_handler(n, registry, config, pipe), "step", n.timeout
        elif n.type == "router" and n.router_mode == "llm":
            labels = [e.when for e in spec.edges if e.src == n.id and e.when]
            handler = llm_router_handler(n, registry, labels, config, spec.defaults)
            kind, timeout = "router", None
        elif n.type == "router":
            handler, kind, timeout = router_handler(n), "router", None
        elif n.type == "gate":
            handler, kind, timeout = gate_handler(n), "gate", None
        else:
            handler, kind, timeout = wait_handler(n, registry), "wait", None
        g.add_node(
            NodeDef(
                id=n.id,
                handler=handler,
                kind=kind,  # type: ignore[arg-type]
                inputs=n.inputs,
                out=n.out,
                max_attempts=n.max_attempts,
                timeout=timeout,
                budget_usd=n.budget_usd,
                meta={"type": n.type, "spec": n},
            )
        )
    for e in spec.edges:
        g.add_edge(EdgeDef(e.src, e.dst, e.when, e.max_loops, e.on_exhausted))
    g.validate()
    return g
