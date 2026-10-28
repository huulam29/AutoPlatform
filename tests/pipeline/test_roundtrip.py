"""Comment-preserving YAML <-> editor model round trip (pipeline/roundtrip.py)."""

from __future__ import annotations

import copy
import difflib
import random
from typing import Any

import pytest

from agentline.pipeline import parse_spec
from agentline.pipeline.roundtrip import apply_model, load_document, rebase, to_model

from .test_spec import EXAMPLE

SOURCE = EXAMPLE.read_text()
BASE = EXAMPLE.parent

COMMENTED = """\
# top comment
version: 1
name: demo  # the name
trigger: { type: manual }

nodes:
  # the first node
  fetch: { type: action, uses: shell.run, with: { cmd: "echo hi" }, out: data }
  think:
    type: agent  # an agent
    runner: python
    prompt: "Use {{data}}"
    in: [data]   # inputs
    out: idea
    with: { function: "mod:fn" }

  # gate before the end
  ok:
    type: gate
    prompt: "Ship it?"

  # done marks the end
  done: { type: action, uses: shell.run, with: { cmd: "true" } }

edges:
  # main line
  - fetch -> think -> ok  # chain comment
  - ok -> done  # tail comment
# trailing comment
"""


def model_of(text: str) -> dict[str, Any]:
    return to_model(text, base_dir=BASE)


def graph(model: dict[str, Any]) -> tuple[Any, ...]:
    """The part of a model that must survive a round trip (no chain indexes, no issues)."""
    nodes = [(n["id"], {"type": n["type"], **n["fields"]}) for n in model["nodes"]]
    edges = sorted(
        tuple(sorted((k, str(v)) for k, v in e.items() if k != "chain_index"))
        for e in model["edges"]
    )
    top = {k: model[k] for k in ("name", "version", "description", "trigger", "budget_usd")}
    return (top, nodes, edges)


def node(model: dict[str, Any], nid: str) -> dict[str, Any]:
    return next(n for n in model["nodes"] if n["id"] == nid)


def changed_lines(a: str, b: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


def edit(text: str, fn: Any) -> tuple[str, list[str], dict[str, Any]]:
    m = copy.deepcopy(to_model(text, validate=False))
    fn(m)
    out, warnings = apply_model(text, m)
    assert graph(to_model(out, validate=False)) == graph(m)
    return out, warnings, m


# -- model ----------------------------------------------------------------------------------
def test_model_of_the_example() -> None:
    m = model_of(SOURCE)
    assert m["name"] == "ticket-to-eod" and m["version"] == 1 and m["budget_usd"] == 5.0
    assert m["trigger"] == {"type": "webhook", "source": "jira", "event": "issue_assigned"}
    assert [n["id"] for n in m["nodes"]][:3] == ["read_ticket", "plan", "implement"]
    plan = node(m, "plan")
    assert plan["type"] == "agent"
    assert plan["fields"]["prompt"] == "prompts/plan.md" and plan["fields"]["in"] == ["ticket"]
    assert node(m, "implement")["fields"]["workspace"] == {"type": "container", "image": "auto"}
    assert len(m["edges"]) == 9
    assert m["edges"][0] == {"src": "read_ticket", "dst": "plan", "chain_index": 0}
    loop = m["edges"][5]
    assert loop == {
        "src": "route",
        "dst": "implement",
        "when": "changes_requested",
        "max_loops": 3,
        "on_exhausted": "needs-review",
        "chain_index": 1,
    }
    assert m["issues"] == []


def test_issues_map_to_nodes_and_edges() -> None:
    text = SOURCE.replace(
        "harness: claude-code\n    prompt: prompts/plan.md",
        "runner: nope\n    prompt: prompts/plan.md",
    )
    text = text.replace(
        "{ from: route, to: approve, when: approved }", "{ from: route, to: ghost, when: approved }"
    )
    m = model_of(text)
    (issue,) = m["issues"]  # unknown nodes stop the validator before per-node checks
    assert issue["edge_indexes"] == [6] and issue["edge_index"] == 6
    assert "ghost" in issue["message"]

    m = model_of(
        SOURCE.replace(
            "harness: claude-code\n    prompt: prompts/plan.md",
            "runner: nope\n    prompt: prompts/plan.md",
        )
    )
    (issue,) = m["issues"]
    assert issue["node_id"] == "plan" and issue["field"] == "runner" and issue["line"]


def test_parse_issues_map_to_yaml_items() -> None:
    text = SOURCE.replace(
        "  - approve -> deploy -> test", "  - { from: approve, to: deploy, bogus: 1 }"
    )
    m = model_of(text)
    (issue,) = m["issues"]
    assert issue["path"] == "edges[3].bogus" and issue["edge_indexes"] == [7]
    m = model_of(SOURCE.replace("    max_turns: 20", "    max_turns: 20\n    colour: red"))
    (issue,) = m["issues"]
    assert issue["node_id"] == "plan" and issue["field"] == "colour"


def test_broken_yaml_gives_an_issue_not_an_exception() -> None:
    m = model_of("nodes: [unclosed\n")
    assert m["nodes"] == [] and m["issues"][0]["path"] == "$"
    with pytest.raises(ValueError):
        apply_model("nodes: [unclosed\n", m)


# -- round trip -----------------------------------------------------------------------------
def test_unchanged_model_is_byte_identical() -> None:
    for text in (SOURCE, COMMENTED):
        out, warnings = apply_model(text, to_model(text, validate=False))
        assert out == text and warnings == []


def test_unchanged_model_via_json_numbers() -> None:
    """The browser sends 5.0 back as 5; that is not a change."""
    m = to_model(SOURCE, validate=False)
    m["budget_usd"] = 5
    node(m, "implement")["fields"]["budget_usd"] = 2
    assert apply_model(SOURCE, m) == (SOURCE, [])


def test_one_field_changes_one_line() -> None:
    out, warnings, _ = edit(SOURCE, lambda m: node(m, "plan")["fields"].update(max_turns=30))
    assert changed_lines(SOURCE, out) == ["-    max_turns: 20", "+    max_turns: 30"]
    assert warnings == []


def test_flow_maps_stay_flow_style_with_original_spacing() -> None:
    out, _, _ = edit(SOURCE, lambda m: node(m, "deploy")["fields"]["with"].update(env="prod"))
    assert changed_lines(SOURCE, out) == ["-    with: { env: eod }", "+    with: { env: prod }"]
    out, _, _ = edit(SOURCE, lambda m: node(m, "deploy")["fields"]["with"].update(dry_run=True))
    assert changed_lines(SOURCE, out) == [
        "-    with: { env: eod }",
        "+    with: { env: eod, dry_run: true }",
    ]
    out, _, _ = edit(SOURCE, lambda m: m["trigger"].update(event="issue_created"))
    assert changed_lines(SOURCE, out) == [
        "-trigger: { type: webhook, source: jira, event: issue_assigned }",
        "+trigger: { type: webhook, source: jira, event: issue_created }",
    ]


def test_flow_style_node_stays_on_one_line() -> None:
    out, warnings, _ = edit(COMMENTED, lambda m: node(m, "fetch")["fields"].update(out="raw"))
    assert changed_lines(COMMENTED, out) == [
        '-  fetch: { type: action, uses: shell.run, with: { cmd: "echo hi" }, out: data }',
        '+  fetch: { type: action, uses: shell.run, with: { cmd: "echo hi" }, out: raw }',
    ]
    assert warnings == []


def test_quoting_is_kept() -> None:
    out, _, _ = edit(
        SOURCE,
        lambda m: node(m, "open_pr")["fields"]["with"].update(title="{{ticket.key}}: fix"),
    )
    assert changed_lines(SOURCE, out) == [
        '-      title: "{{ticket.key}}: {{plan.summary}}"',
        '+      title: "{{ticket.key}}: fix"',
    ]


def test_list_edit_in_place() -> None:
    out, _, _ = edit(SOURCE, lambda m: node(m, "implement")["fields"]["in"].remove("review?"))
    assert changed_lines(SOURCE, out) == [
        "-    in: [ticket, plan, review?]",
        "+    in: [ticket, plan]",
    ]


def test_add_and_remove_a_field() -> None:
    out, _, _ = edit(SOURCE, lambda m: node(m, "read_ticket")["fields"].update(timeout="1m"))
    assert changed_lines(SOURCE, out) == ["+    timeout: 1m"]
    assert "    out: ticket\n    timeout: 1m\n\n  plan:" in out
    out, _, _ = edit(SOURCE, lambda m: node(m, "read_ticket")["fields"].pop("out"))
    assert changed_lines(SOURCE, out) == ["-    out: ticket"]
    assert '"{{payload.issue_key}}" }\n\n  plan:' in out


def test_inline_prompt_becomes_a_block_scalar_and_back() -> None:
    out, _, _ = edit(
        SOURCE, lambda m: node(m, "approve")["fields"].update(prompt="Deploy?\nReally?\n")
    )
    assert "    prompt: |\n      Deploy?\n      Really?\n\n  deploy:" in out
    back, _, _ = edit(out, lambda m: node(m, "approve")["fields"].update(prompt="Deploy?"))
    assert "    prompt: Deploy?\n\n  deploy:" in back


def test_add_a_node_and_an_edge() -> None:
    def add(m: dict[str, Any]) -> None:
        m["nodes"].append(
            {
                "id": "notify",
                "type": "action",
                "fields": {"uses": "jira.comment", "in": ["test_report"], "with": {"body": "ok"}},
            }
        )
        m["edges"].append({"src": "test", "dst": "notify"})

    out, warnings, _ = edit(SOURCE, add)
    assert warnings == []
    assert changed_lines(SOURCE, out) == [
        "+  notify:",
        "+    type: action",
        "+    uses: jira.comment",
        "+    in: [test_report]",
        "+    with: {body: ok}",
        "+  - test -> notify",
    ]
    assert "    out: test_report\n  notify:\n" in out and "{body: ok}\n\nedges:" in out
    parse_spec(out, base_dir=BASE)  # still a valid pipeline


def test_remove_a_node_and_its_edges() -> None:
    def drop(m: dict[str, Any]) -> None:
        m["nodes"] = [n for n in m["nodes"] if n["id"] != "open_pr"]
        m["edges"] = [e for e in m["edges"] if "open_pr" not in (e["src"], e["dst"])]
        m["edges"].append({"src": "implement", "dst": "await_review"})

    out, warnings, _ = edit(SOURCE, drop)
    assert warnings == []
    assert "open_pr" not in out
    assert "  - read_ticket -> plan -> implement -> await_review -> route\n" in out
    assert "      title:" not in out and "\n\n\n" not in out
    assert len(changed_lines(SOURCE, out)) == 8 + 2


def test_remove_the_last_node_keeps_spacing() -> None:
    def drop(m: dict[str, Any]) -> None:
        m["nodes"] = [n for n in m["nodes"] if n["id"] != "test"]
        m["edges"] = [e for e in m["edges"] if e["dst"] != "test"]

    out, _, _ = edit(SOURCE, drop)
    assert "    out: deployment\n\nedges:\n" in out
    assert out.endswith("  - approve -> deploy\n")


def test_insert_a_node_into_a_chain() -> None:
    def insert(m: dict[str, Any]) -> None:
        m["nodes"].append({"id": "lint", "type": "action", "fields": {"uses": "shell.run"}})
        m["edges"] = [e for e in m["edges"] if (e["src"], e["dst"]) != ("plan", "implement")]
        m["edges"] += [{"src": "plan", "dst": "lint"}, {"src": "lint", "dst": "implement"}]

    out, _, _ = edit(SOURCE, insert)
    assert (
        "  - read_ticket -> plan -> lint -> implement -> open_pr -> await_review -> route\n" in out
    )


def test_rename_keeps_position() -> None:
    def rename(m: dict[str, Any]) -> None:
        node(m, "plan").update(id="planner", prev_id="plan")
        for e in m["edges"]:
            e["src"] = "planner" if e["src"] == "plan" else e["src"]
            e["dst"] = "planner" if e["dst"] == "plan" else e["dst"]

    out, _, _ = edit(SOURCE, rename)
    assert changed_lines(SOURCE, out) == [
        "-  plan:",
        "+  planner:",
        "-  - read_ticket -> plan -> implement -> open_pr -> await_review -> route",
        "+  - read_ticket -> planner -> implement -> open_pr -> await_review -> route",
    ]


def test_edge_attribute_edit_in_place() -> None:
    out, _, _ = edit(SOURCE, lambda m: m["edges"][5].update(max_loops=5))
    assert changed_lines(SOURCE, out) == [
        "-  - { from: route, to: implement, when: changes_requested, max_loops: 3, "
        "on_exhausted: needs-review }",
        "+  - { from: route, to: implement, when: changes_requested, max_loops: 5, "
        "on_exhausted: needs-review }",
    ]


def test_new_conditional_edges_are_flow_maps() -> None:
    def add(m: dict[str, Any]) -> None:
        m["edges"].append({"src": "route", "dst": "test", "when": "default"})

    out, _, _ = edit(SOURCE, add)
    assert changed_lines(SOURCE, out) == ["+  - {from: route, to: test, when: default}"]


def test_untouched_chains_are_kept_verbatim() -> None:
    out, _, _ = edit(SOURCE, lambda m: m["edges"].pop(6))  # route -> approve
    assert changed_lines(SOURCE, out) == ["-  - { from: route, to: approve, when: approved }"]


def test_comments_elsewhere_survive() -> None:
    out, warnings, _ = edit(
        COMMENTED, lambda m: node(m, "think")["fields"].update(max_turns=3, runner="shell")
    )
    assert warnings == []
    for line in COMMENTED.splitlines():
        if "#" in line and "runner" not in line:
            assert line in out.splitlines()
    assert changed_lines(COMMENTED, out) == [
        "-    runner: python",
        "+    runner: shell",
        "+    max_turns: 3",
    ]


def test_comments_survive_adding_a_node_and_edge() -> None:
    def add(m: dict[str, Any]) -> None:
        m["nodes"].append({"id": "after", "type": "gate", "fields": {"prompt": "again?"}})
        m["edges"].append({"src": "done", "dst": "after"})

    out, warnings, _ = edit(COMMENTED, add)
    assert warnings == []
    assert out.endswith("  - ok -> done  # tail comment\n  - done -> after\n# trailing comment\n")
    assert '  done: { type: action, uses: shell.run, with: { cmd: "true" } }\n  after:\n' in out


def test_lost_comments_are_reported() -> None:
    def drop(m: dict[str, Any]) -> None:
        m["edges"] = [e for e in m["edges"] if (e["src"], e["dst"]) != ("ok", "done")]

    out, warnings, _ = edit(COMMENTED, drop)
    assert "ok -> done" not in out
    assert len(warnings) == 1 and "# tail comment" in warnings[0]
    assert "# trailing comment" in out and "# main line" in out

    def split(m: dict[str, Any]) -> None:
        m["edges"] = [e for e in m["edges"] if (e["src"], e["dst"]) != ("think", "ok")]
        m["edges"].append({"src": "think", "dst": "done"})

    out, warnings, _ = edit(COMMENTED, split)
    assert "  - fetch -> think  # chain comment\n" in out  # the first piece keeps it
    assert warnings == []


def test_deleting_a_commented_node_warns() -> None:
    def drop(m: dict[str, Any]) -> None:
        m["nodes"] = [n for n in m["nodes"] if n["id"] != "think"]
        m["edges"] = [{"src": "fetch", "dst": "ok"}, {"src": "ok", "dst": "done"}]

    out, warnings, _ = edit(COMMENTED, drop)
    assert "think" not in out
    assert sorted(w.split("'")[1] for w in warnings) == ["# an agent", "# inputs"]
    assert "  # gate before the end\n  ok:" in out
    assert "  - fetch -> ok  # chain comment\n" in out


def test_top_level_fields() -> None:
    out, _, _ = edit(SOURCE, lambda m: m.update(description="Ticket to EOD"))
    assert changed_lines(SOURCE, out) == ["+description: Ticket to EOD"]
    out, _, _ = edit(SOURCE, lambda m: m.update(budget_usd=None))
    assert changed_lines(SOURCE, out) == ["-budget_usd: 5.0"]


def test_rebase_restores_spacing_at_character_level() -> None:
    orig = "a: { x: 1, y: 2 }  # c\nb: 1\n"
    base = "a: {x: 1, y: 2}  # c\nb: 1\n"
    new = "a: {x: 1, y: 3, z: 4}  # c\nb: 2\n"
    assert rebase(orig, base, new) == "a: { x: 1, y: 3, z: 4 }  # c\nb: 2\n"
    assert rebase(orig, base, base) == orig


def test_indentation_is_detected() -> None:
    text = "name: x\nnodes:\n    a:\n        type: gate\n        prompt: hi\nedges:\n- a -> a\n"
    m = to_model(text, validate=False)
    node(m, "a")["fields"]["prompt"] = "bye"
    out, _ = apply_model(text, m)
    assert out == text.replace("prompt: hi", "prompt: bye")
    assert load_document(out)["nodes"]["a"]["prompt"] == "bye"


@pytest.mark.parametrize("seed", range(40))
def test_random_edits_round_trip(seed: int) -> None:
    """Random sequences of edits always give YAML that parses back to the edited model."""
    rnd = random.Random(seed)
    text = rnd.choice([SOURCE, COMMENTED])
    for step in range(4):
        m = to_model(text, validate=False)
        ids = [n["id"] for n in m["nodes"]]
        op = rnd.choice(["field", "add", "drop_node", "drop_edge", "add_edge", "list"])
        if op == "field":
            n = rnd.choice(m["nodes"])
            key = rnd.choice(["description", "timeout", "max_attempts"])
            n["fields"][key] = rnd.choice(["x", 2, "5m", "two words"])
        elif op == "add":
            nid = f"n{seed}_{step}"
            m["nodes"].append({"id": nid, "type": "gate", "fields": {"prompt": "ok?"}})
            m["edges"].append({"src": rnd.choice(ids), "dst": nid})
        elif op == "drop_node" and len(ids) > 2:
            gone = rnd.choice(ids)
            m["nodes"] = [n for n in m["nodes"] if n["id"] != gone]
            m["edges"] = [e for e in m["edges"] if gone not in (e["src"], e["dst"])]
        elif op == "drop_edge" and m["edges"]:
            m["edges"].pop(rnd.randrange(len(m["edges"])))
        elif op == "add_edge":
            m["edges"].append({"src": rnd.choice(ids), "dst": rnd.choice(ids)})
        elif op == "list":
            n = rnd.choice(m["nodes"])
            n["fields"]["in"] = [*n["fields"].get("in", []), "extra"]
        out, _ = apply_model(text, m)
        assert graph(to_model(out, validate=False)) == graph(m), (op, out)
        assert "\n\n\n" not in out, (op, out)
        text = out


# -- defaults (the pipeline's "main node") ----------------------------------------------------
WITH_DEFAULTS = """\
version: 1
name: demo  # the name
trigger: { type: manual }
defaults:  # what agent nodes use
  agent: coder  # Claude Code with a Pro login
  model: opus
  max_turns: 10

# the nodes
nodes:
  think: { type: agent, prompt: "hi" }
"""


def test_model_carries_defaults() -> None:
    assert to_model(SOURCE, validate=False)["defaults"] is None
    m = to_model(WITH_DEFAULTS, validate=False)
    assert m["defaults"] == {"harness": "coder", "model": "opus", "max_turns": 10}


def test_unchanged_defaults_are_byte_identical() -> None:
    m = to_model(WITH_DEFAULTS, validate=False)
    assert apply_model(WITH_DEFAULTS, m) == (WITH_DEFAULTS, [])
    del m["defaults"]  # an older client that doesn't send defaults leaves them alone
    assert apply_model(WITH_DEFAULTS, m) == (WITH_DEFAULTS, [])


def test_add_defaults_keeps_the_blank_line_before_nodes() -> None:
    out, warnings, _ = edit(
        SOURCE, lambda m: m.update(defaults={"agent": "cheap", "model": "free-llama"})
    )
    assert changed_lines(SOURCE, out) == ["+defaults:", "+  agent: cheap", "+  model: free-llama"]
    assert "budget_usd: 5.0\ndefaults:\n  agent: cheap\n  model: free-llama\n\nnodes:\n" in out
    assert warnings == []
    assert parse_spec(out, base_dir=BASE).defaults.model == "free-llama"


def test_change_one_default_changes_one_line() -> None:
    out, warnings, _ = edit(WITH_DEFAULTS, lambda m: m["defaults"].update(model="sonnet"))
    assert changed_lines(WITH_DEFAULTS, out) == ["-  model: opus", "+  model: sonnet"]
    assert warnings == []
    # the old key name (`agent`) is renamed in place once its value changes
    out, warnings, _ = edit(WITH_DEFAULTS, lambda m: m["defaults"].update(harness="cheap"))
    assert changed_lines(WITH_DEFAULTS, out) == [
        "-  agent: coder  # Claude Code with a Pro login",
        "+  harness: cheap  # Claude Code with a Pro login",
    ]


def test_add_and_remove_one_default() -> None:
    out, warnings, _ = edit(WITH_DEFAULTS, lambda m: m["defaults"].pop("max_turns"))
    assert changed_lines(WITH_DEFAULTS, out) == ["-  max_turns: 10"]
    assert "  model: opus\n\n# the nodes\nnodes:" in out and warnings == []
    out2, warnings, _ = edit(out, lambda m: m["defaults"].update(max_turns=5))
    assert changed_lines(out, out2) == ["+  max_turns: 5"]
    assert "  max_turns: 5\n\n# the nodes\nnodes:" in out2 and warnings == []


@pytest.mark.parametrize("gone", [None, {}])
def test_remove_defaults(gone: Any) -> None:
    out, warnings, _ = edit(WITH_DEFAULTS, lambda m: m.update(defaults=gone))
    assert "defaults" not in out
    assert out.startswith("version: 1\nname: demo  # the name\ntrigger: { type: manual }\n")
    assert "\n\n# the nodes\nnodes:" in out
    # comments inside the removed section are reported, not silently lost
    assert any("Pro login" in w for w in warnings)


def test_add_then_remove_defaults_restores_the_file() -> None:
    out, _, _ = edit(SOURCE, lambda m: m.update(defaults={"agent": "x"}))
    back, warnings, _ = edit(out, lambda m: m.update(defaults=None))
    assert back == SOURCE and warnings == []


def test_flow_style_defaults_stay_flow() -> None:
    text = WITH_DEFAULTS.replace(
        "defaults:  # what agent nodes use\n  agent: coder  # Claude Code with a Pro login\n"
        "  model: opus\n  max_turns: 10\n",
        "defaults: { agent: coder, model: opus }\n",
    )
    out, warnings, _ = edit(text, lambda m: m["defaults"].update(model="haiku"))
    assert changed_lines(text, out) == [
        "-defaults: { agent: coder, model: opus }",
        "+defaults: { agent: coder, model: haiku }",
    ]
    assert warnings == []
