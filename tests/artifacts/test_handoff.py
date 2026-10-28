"""Large inputs reach handlers as references; prompts render them as summary + path."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agentline import App, SqliteLedger, StepContext
from agentline.artifacts import ArtifactRef, is_ref
from agentline.pipeline import build_pipeline, parse_spec
from agentline.pipeline.template import render
from agentline.registry import ActionCall, Registry, RunRequest, RunResult
from agentline.runners import FunctionRunner

BIG_TEXT = "\n".join(f"line {i}: something happened" for i in range(4000))  # ~120 KiB

SPEC = """\
name: big
nodes:
  fetch: { type: action, uses: logs.fetch, out: logs }
  analyze:
    type: agent
    runner: fake
    in: [logs]
    out: analysis
    prompt: |
      Investigate.
      {{logs}}
      Kind: {{logs.kind}}
  pick: { type: router, in: [logs], switch: logs.kind }
  finish: { type: action, uses: logs.finish, in: [analysis] }
edges:
  - fetch -> analyze -> pick
  - { from: pick, to: finish, when: text }
"""


def build(tmp_path: Path, seen: dict[str, Any], **app_kw: Any) -> App:
    reg = Registry()

    async def agent(req: RunRequest) -> RunResult:
        seen["prompt"], seen["inputs"] = req.prompt, req.inputs
        return RunResult(output={"verdict": "ok"})

    reg.add_runner(FunctionRunner("fake", agent))

    @reg.action("logs.fetch")
    async def fetch(ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        return {"kind": "text", "body": BIG_TEXT}

    @reg.action("logs.finish")
    async def finish(ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        return {"done": True}

    app = App(
        SqliteLedger(tmp_path / "ledger.db"),
        backoff_base=0.0,
        artifact_root=tmp_path / "artifacts",
        **app_kw,
    )
    app.register_pipeline(build_pipeline(parse_spec(SPEC), reg))
    return app


def test_large_input_is_a_reference_rendered_as_summary_and_path(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    app = build(tmp_path, seen)
    sub = app.submit_sync("big", {})
    asyncio.run(app.worker().run_until_idle())
    assert {x.node_id: x.status for x in app.executions(sub.run_id)} == {
        "fetch": "done",
        "analyze": "done",
        "pick": "done",  # the router read `logs.kind` through the reference
        "finish": "done",
    }
    ref = seen["inputs"]["logs"]
    assert is_ref(ref)
    assert set(ref) == {"$artifact", "summary", "size", "media_type", "path"}
    assert ref["media_type"] == "application/json" and ref["size"] > 64 * 1024
    assert ref["summary"] == '{kind: "text", body: "line 0: something happened"}'
    path = Path(ref["path"])
    assert path.is_file() and path.is_relative_to(tmp_path / "artifacts")

    prompt = seen["prompt"]
    assert f"{ref['summary']}\n(full content: {path})" in prompt
    assert "Kind: text" in prompt  # field access reads the referenced file
    assert "line 3999" not in prompt and len(prompt) < 1000

    assert app.result(sub.run_id, "fetch")["body"] == BIG_TEXT  # full value still readable


def test_small_inputs_stay_inline(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    app = build(tmp_path, seen, handoff_max=1024 * 1024)
    app.submit_sync("big", {})
    asyncio.run(app.worker().run_until_idle())
    assert seen["inputs"]["logs"]["body"] == BIG_TEXT
    assert "line 3999" in seen["prompt"]


def test_ctx_load_dereferences_on_demand(tmp_path: Path) -> None:
    app = App(
        SqliteLedger(tmp_path / "ledger.db"),
        artifact_root=tmp_path / "artifacts",
        inline_max=1024,
        handoff_max=100,
    )
    flow = app.flow("f")
    got: dict[str, Any] = {}

    @flow.step("make")
    async def make(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        return {"items": list(range(500))}

    @flow.step("use", after="make")
    async def use(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        got["raw"] = inputs["make"]
        got["full"] = await ctx.load(inputs["make"])
        got["same"] = await ctx.load({"small": 1})  # non-references pass through
        return {"n": len(got["full"]["items"])}

    sub = app.submit_sync("f", None)
    asyncio.run(app.worker().run_until_idle())
    ref = ArtifactRef.from_value(got["raw"])
    assert ref.summary == "{items: [500 items]}"
    assert got["full"] == {"items": list(range(500))} and got["same"] == {"small": 1}
    assert app.result(sub.run_id, "use") == {"n": 500}
    make_ex = next(x for x in app.executions(sub.run_id) if x.node_id == "make")
    art = app.artifact(make_ex.result_ref or "")
    assert art is not None and art["storage"] == "file"  # over inline_max: stored as a file


def test_template_renders_refs_and_missing_files() -> None:
    ref = {"$artifact": "ab" * 32, "summary": "S", "size": 1, "media_type": "x", "path": "/nope"}
    assert render("{{r}}", {"r": ref}) == "S\n(full content: /nope)"
    assert render("[{{r.field?}}]", {"r": ref}) == "[]"
