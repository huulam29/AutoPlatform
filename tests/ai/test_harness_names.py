"""Phase 35: `[agents]` -> `[harnesses]`, `agent:` -> `harness:`, `llm` -> `builtin`. The old
names are still read everywhere a user file or an older client could carry them."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from agentline import agents as ai
from agentline import setup
from agentline.config import Config
from agentline.errors import ValidationError
from agentline.pipeline import SpecError, parse_spec
from agentline.pipeline.roundtrip import apply_model, to_model
from agentline.tomlwrite import delete_table, migrate_section, set_table

from .conftest import OPENROUTER_TOML

BASE = OPENROUTER_TOML.format(base="https://or.test/v1")


def test_old_agents_table_and_llm_kind_are_read() -> None:
    c = Config.from_dict(tomllib.loads(BASE + '\n[agents.small]\nkind = "llm"\nmodel = "cheap"\n'))
    assert c.harnesses["small"].kind == "builtin"
    assert ai.resolve("small", None, c).runner == "llm"
    bare = Config.from_dict(tomllib.loads(BASE + '\n[agents.llm]\nmodel = "cheap"\n'))
    assert bare.harnesses["llm"].kind == "builtin"  # kind defaults to the name: the old one


def test_the_same_name_in_both_tables_is_an_error() -> None:
    raw = '[agents.x]\nkind = "codex"\n[harnesses.x]\nkind = "aider"\n'
    with pytest.raises(ValidationError, match="both"):
        Config.from_dict(tomllib.loads(raw))


def test_llm_resolves_to_builtin_unless_a_profile_has_that_name() -> None:
    c = Config.from_dict(tomllib.loads(BASE))
    assert ai.resolve("llm", "cheap", c).harness.name == "builtin"
    own = Config.from_dict(tomllib.loads(BASE + '\n[harnesses.llm]\nkind = "aider"\n'))
    assert ai.resolve("llm", None, own).harness.kind == "aider"


def test_pipeline_agent_field_is_read_as_harness() -> None:
    spec = parse_spec(
        "name: p\ndefaults: { agent: llm, model: cheap }\n"
        "nodes:\n  a: { type: agent, agent: claude-code, prompt: hi }\n"
    )
    assert spec.defaults.harness == "llm" and spec.nodes["a"].harness == "claude-code"
    with pytest.raises(SpecError, match="old name of 'harness'"):
        parse_spec("name: p\nnodes:\n  a: { type: agent, agent: x, harness: y, prompt: hi }\n")


def test_editor_model_shows_harness_and_saves_it_only_when_changed() -> None:
    text = (
        "name: p\n"
        "nodes:\n"
        "  a:\n"
        "    type: agent\n"
        "    agent: claude-code  # the login\n"
        "    prompt: hi\n"
        "  b:\n"
        "    type: agent\n"
        "    agent: codex\n"
        "    prompt: there\n"
    )
    m = to_model(text, validate=False)
    fields = {n["id"]: n["fields"] for n in m["nodes"]}
    assert fields["a"]["harness"] == "claude-code" and "agent" not in fields["a"]
    assert apply_model(text, m) == (text, [])  # untouched: byte-identical
    fields["a"]["harness"] = "builtin"
    out, _ = apply_model(text, m)
    line = next(x for x in out.splitlines() if "harness:" in x)
    assert line.startswith("    harness: builtin ") and line.endswith("# the login")  # in place
    assert "    agent: codex\n" in out  # the node nobody touched keeps its old key


def test_settings_write_renames_old_headers_in_place() -> None:
    text = '# mine\n[agents.small]  # cheap one\nkind = "llm"\n\n[models.m]\nprovider = "x"\n'
    out = set_table(text, "harnesses", "big", {"kind": "codex"})
    assert "[harnesses.small]  # cheap one\n" in out and "[agents." not in out
    assert out.startswith("# mine\n")
    parsed = tomllib.loads(out)
    assert parsed["harnesses"] == {"small": {"kind": "llm"}, "big": {"kind": "codex"}}
    assert parsed["models"] == {"m": {"provider": "x"}}
    assert "[agents." not in delete_table(text, "harnesses", "small")
    assert migrate_section(text, "models") == text


def test_old_saved_chat_choice_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_HOME", str(tmp_path))
    (tmp_path / "setup.json").write_text(
        json.dumps({"assistant": {"agent": "llm", "model": "free"}})
    )
    assert setup.read_state()["assistant"] == {"harness": "builtin", "model": "free"}


def test_old_bundle_requirements_are_read() -> None:
    from agentline.bundle import missing

    got = missing({"agents": ["llm", "ghost"]}, Config.from_dict(tomllib.loads(BASE)))
    assert got["harnesses"] == ["ghost"]
