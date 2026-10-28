"""The llm runner against a fake OpenAI-compatible server, pipeline defaults, per-node agent
and model choice, and LLM routers, end to end on the engine."""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from typing import Any

import pytest

from agentline import App, SqliteLedger
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.errors import PermanentError
from agentline.pipeline import build_pipeline, parse_spec, validate_spec
from agentline.registry import Limits, RunRequest
from agentline.runners.common import RunnerError
from agentline.runners.llm import LLMRunner

from .conftest import OPENROUTER_TOML
from .fakes import KEY, FakeLLM, fake_llm


def config(base: str) -> Config:
    return Config.from_dict(tomllib.loads(OPENROUTER_TOML.format(base=base)))


def request(base: str, **params: Any) -> RunRequest:
    p = {
        "provider_type": "openai-compatible",
        "base_url": base,
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "price_unknown_ok": True,
        **params,
    }
    return RunRequest(prompt="Summarize", inputs={}, limits=Limits(), params=p)


def test_llm_runner_calls_chat_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    with fake_llm(FakeLLM(replies=['```json\n{"summary": "ok"}\n```'])) as (base, st):
        res = asyncio.run(LLMRunner().run(request(base, headers={"X-Title": "agentline"})))
    assert res.output == {"summary": "ok"}
    assert res.cost_usd == 0 and "warnings" not in res.meta
    assert st.requests[0]["model"] == "meta-llama/llama-3.3-70b-instruct:free"
    assert st.requests[0]["messages"] == [{"role": "user", "content": "Summarize"}]
    assert st.headers[0]["X-Title"] == "agentline"


def test_llm_runner_costs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    with fake_llm(FakeLLM(replies=["a", "b", "c"])) as (base, st):
        priced = asyncio.run(
            LLMRunner().run(request(base, input_usd_per_mtok=2.0, output_usd_per_mtok=8.0))
        )
        st.usage_cost = 0.0123
        reported = asyncio.run(LLMRunner().run(request(base, model="x/paid")))
        st.usage_cost = None
        unknown = asyncio.run(
            LLMRunner().run(request(base, model="x/paid", price_unknown_ok=False))
        )
    assert priced.cost_usd == pytest.approx((1000 * 2 + 500 * 8) / 1e6)
    assert reported.cost_usd == pytest.approx(0.0123)
    assert unknown.cost_usd == 0 and "no price known" in unknown.meta["warnings"][0]


def test_llm_runner_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        fake_llm() as (base, _),
        pytest.raises(PermanentError, match="OPENROUTER_API_KEY is not set"),
    ):
        asyncio.run(LLMRunner().run(request(base)))
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    rate_limited = FakeLLM(replies=[(429, {"error": "rate limited"}), (400, {"error": "bad"})])
    with fake_llm(rate_limited) as (base, _):
        with pytest.raises(RunnerError, match="429"):  # retryable
            asyncio.run(LLMRunner().run(request(base)))
        with pytest.raises(PermanentError, match="400"):
            asyncio.run(LLMRunner().run(request(base)))


def test_private_server_without_key() -> None:
    with fake_llm(FakeLLM(require_key=None, replies=["hi"])) as (base, _):
        res = asyncio.run(LLMRunner().run(request(base, api_key_env=None)))
    assert res.output == "hi"


PIPELINE = """\
name: triage
defaults: {{ harness: builtin, model: cheap }}
nodes:
  classify:
    type: router
    mode: llm
    prompt: "Is this a bug or a feature request? {{{{payload.text}}}}"
  summarize:
    type: agent
    prompt: "Summarize the bug: {{{{payload.text}}}}"
    out: summary
  plan:
    type: agent
    harness: claude-code
    {plan_model}
    prompt: "Plan the feature: {{{{payload.text}}}}"
edges:
  - {{ from: classify, to: summarize, when: bug }}
  - {{ from: classify, to: plan, when: feature }}
"""


def build(base: str, tmp_path: Path, plan_model: str = "") -> tuple[App, Any]:
    cfg = config(base)
    spec = parse_spec(PIPELINE.format(plan_model=plan_model))
    reg = default_registry(cfg)
    app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0)
    app.register_pipeline(build_pipeline(spec, reg, cfg))
    return app, spec


def test_defaults_and_llm_router_on_openrouter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)

    def reply(body: dict[str, Any]) -> str:
        text = body["messages"][-1]["content"]
        return "Bug." if "Decide which branch" in text else "Login fails on empty passwords."

    with fake_llm(FakeLLM(reply=reply)) as (base, st):
        app, _ = build(base, tmp_path)
        sub = app.submit_sync("triage", {"text": "Login crashes"})
        asyncio.run(app.worker(concurrency=1).run_until_idle())
    keys = {x.key: x.status for x in app.executions(sub.run_id)}
    assert keys == {"classify#1": "done", "summarize#1": "done", "plan#1": "skipped"}
    assert app.result(sub.run_id, "classify")["route"] == "bug"
    assert app.result(sub.run_id, "summarize") == "Login fails on empty passwords."
    router_prompt = st.requests[0]["messages"][-1]["content"]
    assert "- bug\n- feature" in router_prompt
    summarize = next(x for x in app.executions(sub.run_id) if x.node_id == "summarize")
    assert summarize.detail["harness"] == "builtin"
    assert summarize.detail["model"]["name"] == "cheap"


def test_llm_router_repairs_then_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    with fake_llm(FakeLLM(replies=["maybe?", "feature"])) as (base, st):
        app, _ = build(base, tmp_path, plan_model="model: opus")
        sub = app.submit_sync("triage", {"text": "Add dark mode"})
        asyncio.run(app.worker(concurrency=1).run_until_idle())
    assert app.result(sub.run_id, "classify")["route"] == "feature"
    assert "is not one of the labels" in st.requests[1]["messages"][-1]["content"]


def test_defaults_are_a_pair() -> None:
    from agentline.agents import choose

    assert choose(None, None, "llm", "cheap") == ("llm", "cheap")
    assert choose("claude-code", None, "llm", "cheap") == ("claude-code", None)
    assert choose("llm", None, "llm", "cheap") == ("llm", "cheap")
    assert choose(None, "fast", "llm", "cheap") == ("llm", "fast")


def test_claude_code_with_openrouter_model_is_a_validation_error(tmp_path: Path) -> None:
    cfg = config("https://or.test/v1")
    spec = parse_spec(PIPELINE.format(plan_model="model: cheap"))
    issues = [str(i) for i in validate_spec(spec, default_registry(cfg), cfg)]
    assert any(
        "nodes.plan" in i and "Claude Code uses Anthropic models only" in i and ":12:" in i
        for i in issues
    ), issues
    ok = parse_spec(PIPELINE.format(plan_model="model: opus"))
    assert validate_spec(ok, default_registry(cfg), cfg) == []


def test_spec_errors_for_agent_fields() -> None:
    from agentline.pipeline import SpecError

    bad = """\
name: p
defaults: { agent: llm, modle: x }
nodes:
  a: { type: agent, agent: llm, runner: python, prompt: hi }
  r: { type: router, mode: llm, switch: a.x }
  s: { type: router, prompt: "pick" }
"""
    with pytest.raises(SpecError) as e:
        parse_spec(bad)
    text = str(e.value)
    assert "defaults.modle" in text and "did you mean 'model'" in text
    assert "use either 'harness'" in text
    assert "an llm router decides by prompt, not switch" in text
    assert "'prompt' needs mode: llm" in text
