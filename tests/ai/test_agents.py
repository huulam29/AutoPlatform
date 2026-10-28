"""Config parsing for providers/models/harnesses, and harness + model resolution."""

from __future__ import annotations

import tomllib

import pytest

from agentline.agents import KINDS, resolve, summary
from agentline.config import Config
from agentline.errors import ValidationError

from .conftest import OPENROUTER_TOML


def cfg(extra: str = "") -> Config:
    return Config.from_dict(
        tomllib.loads(OPENROUTER_TOML.format(base="https://or.test/v1") + extra)
    )


def test_config_parses_providers_models_harnesses() -> None:
    c = cfg('\n[harnesses.reviewer]\nkind = "builtin"\nmodel = "cheap"\n')
    assert c.providers["openrouter"].type == "openai-compatible"
    assert c.providers["claude"].auth == "subscription"
    assert c.models["cheap"].model == "meta-llama/llama-3.3-70b-instruct:free"
    assert c.harnesses["reviewer"].kind == "builtin" and c.harnesses["reviewer"].model == "cheap"


@pytest.mark.parametrize(
    ("toml", "error"),
    [
        ('[providers.x]\ntype = "grpc"\n', "type must be one of"),
        ('[providers.x]\ntype = "openai-compatible"\n', "needs 'base_url'"),
        ('[providers.x]\ntype = "openai-compatible"\nbase_url = "ftp://a"\n', "http://"),
        (
            '[providers.x]\ntype = "openai-compatible"\nbase_url = "https://a"\n'
            'auth = "subscription"\n',
            "anthropic only",
        ),
        (
            '[providers.x]\ntype = "anthropic"\napi_key_env = "sk-ant-123"\n',
            "env var name, not a key",
        ),
        ('[models.m]\nprovider = "x"\n', "needs 'model'"),
        ('[harnesses.a]\nextra_args = "--x"\n', "list of strings"),
    ],
)
def test_bad_config_is_rejected(toml: str, error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        Config.from_dict(tomllib.loads(toml))


def test_claude_code_default_uses_its_own_model() -> None:
    r = resolve("claude-code", None, None)
    assert r.runner == "claude-code" and r.model is None and r.params == {}


def test_claude_code_with_subscription_model_passes_no_api_key() -> None:
    r = resolve("claude-code", "opus", cfg())
    assert r.params == {"env": [], "model": "claude-opus-5-5"}


def test_claude_code_with_api_key_provider_maps_the_key() -> None:
    c = cfg('\n[providers.work]\ntype = "anthropic"\napi_key_env = "WORK_ANTHROPIC_KEY"\n')
    r = resolve("claude-code", "work:claude-sonnet-5-5", c)
    assert r.params["env_map"] == {"ANTHROPIC_API_KEY": "WORK_ANTHROPIC_KEY"}
    assert r.params["model"] == "claude-sonnet-5-5"


def test_claude_code_respects_configured_subscription_without_a_model() -> None:
    c = Config.from_dict(
        tomllib.loads('[providers.anthropic]\ntype = "anthropic"\nauth = "subscription"\n')
    )
    assert resolve("claude-code", None, c).params == {"env": []}


def test_claude_code_rejects_openrouter_models() -> None:
    with pytest.raises(ValidationError, match="Claude Code uses Anthropic models only"):
        resolve("claude-code", "cheap", cfg())


def test_llm_with_openrouter_free_model() -> None:
    r = resolve("builtin", "cheap", cfg())
    assert r.runner == "llm"
    assert r.params["base_url"] == "https://or.test/v1"
    assert r.params["api_key_env"] == "OPENROUTER_API_KEY"
    assert r.params["price_unknown_ok"] is True


def test_model_without_agent_means_llm_and_provider_colon_id() -> None:
    r = resolve(None, "openrouter:qwen/qwen3-8b:free", cfg())
    assert r.runner == "llm" and r.params["model"] == "qwen/qwen3-8b:free"


def test_subscription_provider_only_for_claude_code() -> None:
    with pytest.raises(ValidationError, match="only claude-code"):
        resolve("builtin", "opus", cfg())


def test_builtin_defaults_need_only_keys() -> None:
    llm = resolve("builtin", None, None)  # no config at all
    assert (
        llm.params["model"] == "openrouter/free"
        and llm.params["api_key_env"] == "OPENROUTER_API_KEY"
    )
    assert llm.params["input_usd_per_mtok"] == 0.0
    cc = resolve("claude-code", "opus", None)
    assert cc.params == {"env": [], "model": "claude-opus-5-5"}  # your login, no API key
    assert resolve("aider", None, None).params["env_map"] == {
        "ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY"
    }
    assert resolve("codex", None, None).params["model"] == "gpt-6.1-sol"
    from agentline.config import HarnessProfile

    c = Config(harnesses={"bare": HarnessProfile("bare", "builtin")})
    with pytest.raises(ValidationError, match="needs a model"):
        resolve("bare", None, c)


def test_llm_unknown_names() -> None:
    with pytest.raises(ValidationError, match="unknown harness 'gpt-agent'"):
        resolve("gpt-agent", None, cfg())
    with pytest.raises(ValidationError, match="not a \\[models\\] name"):
        resolve("builtin", "nope", cfg())


def test_aider_and_codex_with_openrouter() -> None:
    a = resolve("aider", "cheap", cfg())
    assert a.params["model"] == "openai/meta-llama/llama-3.3-70b-instruct:free"
    assert a.params["extra_args"] == ["--openai-api-base", "https://or.test/v1"]
    assert a.params["env_map"] == {"OPENAI_API_KEY": "OPENROUTER_API_KEY"}
    c = resolve("codex", "cheap", cfg())
    assert 'model_providers.agentline.base_url="https://or.test/v1"' in c.params["extra_args"]
    assert c.params["env_map"] == {"AGENTLINE_PROVIDER_KEY": "OPENROUTER_API_KEY"}


def test_agent_profile_default_model_and_bin() -> None:
    c = cfg(
        '\n[harnesses.small]\nkind = "builtin"\nmodel = "cheap"\n'
        '[harnesses.cc]\nkind = "claude-code"\nbin = "/opt/claude"\n'
    )
    assert resolve("small", None, c).params["model"] == "meta-llama/llama-3.3-70b-instruct:free"
    assert resolve("cc", None, c).params["bin"] == "/opt/claude"


def test_summary_lists_compatible_models_per_agent() -> None:
    s = summary(cfg())
    assert set(s["kinds"]) == set(KINDS)
    assert s["compatible_models"]["claude-code"] == ["opus", "sonnet", "sonnet-api", "haiku-api"]
    assert set(s["compatible_models"]["builtin"]) == {
        "sonnet-api", "haiku-api", "free", "auto", "gpt", "cheap"
    }  # fmt: skip
    assert {m["name"]: m["implicit"] for m in s["models"]}["cheap"] is False
    assert {m["name"]: m["implicit"] for m in s["models"]}["free"] is True
    assert {p["name"]: p["implicit"] for p in s["providers"]}["anthropic"] is True
