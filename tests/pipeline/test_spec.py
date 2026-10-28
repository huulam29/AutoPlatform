"""Parser and validator: every error names the field, the line and a hint where possible."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentline.pipeline import SpecError, load_spec, parse_spec, validate_spec
from agentline.pipeline.spec import parse_duration

from .fakes import World, make_registry

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "ticket-to-eod" / "pipeline.yaml"

MINIMAL = """\
version: 1
name: p
nodes:
  a: { type: action, uses: jira.get_ticket, with: { key: X } }
  b: { type: action, uses: env.test, in: [a] }
edges:
  - a -> b
"""


def issues(source: str, registry: bool = True) -> list[str]:
    try:
        spec = parse_spec(source, base_dir=EXAMPLE.parent)
    except SpecError as e:
        return [str(i) for i in e.issues]
    reg = make_registry(World()) if registry else None
    return [str(i) for i in validate_spec(spec, reg)]


def test_example_pipeline_is_valid() -> None:
    spec = load_spec(EXAMPLE)
    assert validate_spec(spec, make_registry(World())) == []
    assert spec.nodes["plan"].prompt and "{{ticket.key}}" in spec.nodes["plan"].prompt
    assert spec.nodes["implement"].timeout == 1800
    assert spec.nodes["route"].inputs == ("review",)
    loop = [e for e in spec.edges if e.max_loops]
    assert [(e.src, e.dst, e.max_loops) for e in loop] == [("route", "implement", 3)]


def test_minimal_is_valid() -> None:
    assert issues(MINIMAL) == []


def test_invalid_yaml_reports_line() -> None:
    (msg,) = issues("name: p\nnodes:\n  a: [unclosed\n")
    assert "invalid YAML" in msg and ":4:" in msg


def test_unknown_field_suggests_fix_with_line() -> None:
    src = MINIMAL.replace("uses: env.test", "usse: env.test")
    out = issues(src)
    assert any("nodes.b.usse: field not allowed" in m and "did you mean 'uses'" in m for m in out)
    assert any(":5:" in m for m in out)


def test_missing_required_field() -> None:
    src = MINIMAL.replace("uses: env.test, ", "")
    assert any("action nodes need 'uses'" in m for m in issues(src))


def test_unknown_node_type() -> None:
    src = MINIMAL.replace("type: action, uses: env.test", "type: agnet")
    assert any("unknown node type 'agnet'" in m for m in issues(src))


def test_condition_in_chain_edge_is_rejected() -> None:
    src = MINIMAL.replace("- a -> b", "- a -[x]-> b")
    assert any("conditions aren't allowed in chain edges" in m for m in issues(src))


def test_edge_to_unknown_node() -> None:
    src = MINIMAL.replace("- a -> b", "- a -> c")
    assert any("unknown node 'c'" in m and ":7:" in m for m in issues(src))


def test_cycle_without_max_loops() -> None:
    src = MINIMAL.replace("  - a -> b\n", "  - a -> b\n  - b -> a\n")
    assert any("cycle without max_loops: a -> b -> a" in m for m in issues(src))


def test_input_not_on_every_path() -> None:
    src = """\
name: p
nodes:
  r: { type: router, switch: payload.kind }
  x: { type: action, uses: env.test, out: data }
  y: { type: action, uses: env.test }
  z: { type: action, uses: env.test, in: [data] }
edges:
  - { from: r, to: x, when: one }
  - { from: r, to: y, when: two }
  - x -> z
  - y -> z
"""
    out = issues(src)
    assert any("input 'data' isn't produced on every path" in m and "data?" in m for m in out)
    assert issues(src.replace("in: [data]", "in: [data?]")) == []


def test_template_reference_must_be_declared() -> None:
    src = MINIMAL.replace("in: [a] }", "in: [a], with: { note: '{{ticket.key}}' } }")
    assert any("template refers to 'ticket'" in m for m in issues(src))


def test_deploy_needs_a_gate_before_it() -> None:
    src = """\
name: p
nodes:
  build: { type: action, uses: env.test, out: branch }
  deploy: { type: action, uses: env.deploy, in: [branch], with: { env: eod } }
edges:
  - build -> deploy
"""
    assert any("can run without passing a gate" in m for m in issues(src))
    gated = src.replace("  deploy:", "  ok: { type: gate, prompt: Ship? }\n  deploy:").replace(
        "build -> deploy", "build -> ok -> deploy"
    )
    assert issues(gated) == []


def test_ai_on_unknown_not_allowed_for_deploy() -> None:
    src = """\
name: p
nodes:
  ok: { type: gate, prompt: Ship? }
  deploy: { type: action, uses: env.deploy, effect: { on_unknown: ai } }
edges:
  - ok -> deploy
"""
    assert any("on_unknown: ai is not allowed on deploy-class" in m for m in issues(src))


def test_unknown_runner_and_action_list_known_ones() -> None:
    src = MINIMAL.replace("uses: env.test", "uses: env.tset")
    assert any("unknown action 'env.tset'" in m and "env.test" in m for m in issues(src))


def test_missing_prompt_file() -> None:
    src = """\
name: p
nodes:
  a: { type: agent, runner: claude-code, prompt: prompts/nope.md }
"""
    assert any("prompt file 'prompts/nope.md' not found" in m for m in issues(src))


def test_inline_prompt_and_unreachable_node() -> None:
    src = """\
name: p
nodes:
  a: { type: agent, runner: claude-code, prompt: "Say hi to {{payload.name}}" }
  b: { type: action, uses: env.test }
  c: { type: action, uses: env.test }
edges:
  - a -> b
  - c -> b
"""
    assert issues(src) == []
    src2 = src.replace("  - c -> b\n", "  - c -> c\n")
    out = issues(src2)
    assert any("cycle without max_loops: c -> c" in m for m in out)


def test_loop_body_must_not_join_outside_node() -> None:
    src = """\
name: p
nodes:
  side: { type: action, uses: env.test }
  work: { type: action, uses: env.test }
  check: { type: action, uses: env.test }
  r: { type: router, switch: check.state, in: [check] }
  done: { type: action, uses: env.test }
edges:
  - work -> check -> r
  - side -> check
  - { from: r, to: work, when: again, max_loops: 2 }
  - { from: r, to: done, when: ok }
"""
    assert any("joins with 'side' outside it" in m for m in issues(src))


def test_max_loops_on_forward_edge() -> None:
    src = """\
name: p
nodes:
  r: { type: router, switch: payload.x }
  a: { type: action, uses: env.test }
edges:
  - { from: r, to: a, when: go, max_loops: 2 }
"""
    assert any("doesn't loop back" in m for m in issues(src))


def test_router_edges_need_when() -> None:
    src = """\
name: p
nodes:
  r: { type: router, switch: payload.x }
  a: { type: action, uses: env.test }
edges:
  - r -> a
"""
    assert any("needs 'when'" in m for m in issues(src))


@pytest.mark.parametrize(("text", "seconds"), [(30, 30), ("45s", 45), ("10m", 600), ("2h", 7200)])
def test_durations(text: object, seconds: float) -> None:
    assert parse_duration(text) == seconds


def test_spec_error_lists_all_issues_at_once() -> None:
    src = MINIMAL.replace("uses: env.test", "usse: env.test").replace("name: p", "name: ''")
    with pytest.raises(SpecError) as exc:
        parse_spec(src)
    assert len(exc.value.issues) >= 3


def test_extra_unconditional_edge_into_a_node_keeps_its_inputs_guaranteed() -> None:
    # read_ticket -> implement next to read_ticket -> plan -> implement: implement waits for both,
    # and plan always runs, so 'plan' is still guaranteed (a user hit a false error here).
    src = EXAMPLE.read_text() + "  - read_ticket -> implement\n"
    spec = parse_spec(src, base_dir=EXAMPLE.parent)
    assert validate_spec(spec, make_registry(World())) == []


def test_input_after_a_router_branch_is_still_not_guaranteed() -> None:
    src = """\
name: p
nodes:
  r: { type: router, switch: payload.kind }
  x: { type: action, uses: env.test, out: data }
  z: { type: action, uses: env.test, in: [data] }
  w: { type: action, uses: env.test }
edges:
  - { from: r, to: x, when: one }
  - { from: r, to: w, when: two }
  - x -> z
  - w -> z
"""
    assert any("isn't produced on every path" in m for m in issues(src))
