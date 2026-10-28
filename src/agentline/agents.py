"""Harnesses and model sources: which harness runs a node, and with which model.

Three separate things, all from agentline.toml (and editable in the GUI's Settings page):
- **providers**: where models come from (`anthropic`, or `openai-compatible` for OpenRouter,
  OpenAI, Ollama, vLLM, a private server);
- **models**: named model choices, `[models.cheap] provider = "openrouter", model = "..."`;
- **harnesses**: what drives the model (the agent loop, its tools and context), of a kind:
  `claude-code`, `codex`, `aider`, `opencode`, `hermes` (CLIs), or `builtin` (agentline's own:
  a direct model call on pipeline nodes, a small tool loop in the Chat; needs only an API
  key). Before phase 35 these were `[agents]` and `kind = "llm"`; both are still read.

A node picks `harness:` and/or `model:`; the pipeline's `defaults:` fill in what a node leaves
out. Each harness kind declares the provider types it can use, so incompatible pairs are a
validation error. Claude Code uses Anthropic only (its own login or an API key); OpenRouter
and private servers are for `builtin`, LLM routers, Codex and Aider.

`model:` is a `[models]` name, or `provider:model-id` (e.g. `openrouter:meta-llama/llama-3.3-70b-
instruct:free`; split at the first colon), or a bare model id for the harness's default
provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from agentline.config import HARNESS_KIND_ALIASES, Config, HarnessProfile, ModelProfile, Provider
from agentline.errors import ValidationError


@dataclass(frozen=True)
class HarnessKind:
    name: str
    runner: str
    provider_types: tuple[str, ...]
    needs_model: bool
    description: str
    default_bin: str | None = None
    subscription: bool = False  # can use a Claude subscription login


KINDS: dict[str, HarnessKind] = {
    "claude-code": HarnessKind(
        "claude-code",
        "claude-code",
        ("anthropic",),
        needs_model=False,
        description="Claude Code (headless `claude -p`). Anthropic models only: its own login"
        " (Pro/Max subscription) or an Anthropic API key.",
        default_bin="claude",
        subscription=True,
    ),
    "codex": HarnessKind(
        "codex",
        "codex",
        ("openai-compatible",),
        needs_model=False,
        description="OpenAI Codex CLI (`codex exec`). OpenAI or any OpenAI-compatible provider.",
        default_bin="codex",
    ),
    "aider": HarnessKind(
        "aider",
        "aider",
        ("anthropic", "openai-compatible"),
        needs_model=False,
        description="Aider (`aider --message`). Anthropic or any OpenAI-compatible provider.",
        default_bin="aider",
    ),
    "opencode": HarnessKind(
        "opencode",
        "opencode",
        ("anthropic", "openai-compatible"),
        needs_model=False,
        description="OpenCode (`opencode run`). Anthropic or any OpenAI-compatible provider;"
        " with no model it uses its own default.",
        default_bin="opencode",
    ),
    "hermes": HarnessKind(
        "hermes",
        "hermes",
        ("anthropic", "openai-compatible"),
        needs_model=False,
        description="Nous Research's Hermes Agent (`hermes chat -q`). Anthropic, OpenRouter or"
        " any OpenAI-compatible provider; with no model it uses its own default.",
        default_bin="hermes",
    ),
    "builtin": HarnessKind(
        "builtin",
        "llm",
        ("anthropic", "openai-compatible"),
        needs_model=True,
        description="agentline's own harness: works with just an API key (OpenRouter, OpenAI,"
        " Anthropic, a private server). On pipeline nodes a direct model call, no tools; in the"
        " Chat a small tool loop.",
    ),
}

# Built-in defaults, so a new install only needs keys. Each is used unless agentline.toml
# defines an entry with the same name (Settings edits create that override). Model ids checked
# against the providers' model lists on 2026-10-19; `openrouter/free` routes to whatever free
# models OpenRouter has, so it doesn't go stale when individual free models come and go.
BUILTIN_PROVIDERS: dict[str, Provider] = {
    p.name: p
    for p in (
        Provider(
            name="claude",
            type="anthropic",
            auth="subscription",
            description="Your Claude Pro/Max login, through Claude Code (no key needed)",
        ),
        Provider(
            name="anthropic",
            type="anthropic",
            api_key_env="ANTHROPIC_API_KEY",
            description="Anthropic API, billed per token",
        ),
        Provider(
            name="openrouter",
            type="openai-compatible",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="OPENROUTER_API_KEY",
            headers={"X-Title": "agentline"},
            description="OpenRouter: hundreds of models, including free ones",
        ),
        Provider(
            name="openai",
            type="openai-compatible",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            description="OpenAI API",
        ),
        Provider(
            name="ollama",
            type="openai-compatible",
            base_url="http://localhost:11434/v1",
            description="Ollama on this machine (no key; pick a model you've pulled)",
        ),
    )
}
DEFAULT_ANTHROPIC = BUILTIN_PROVIDERS["anthropic"]

BUILTIN_MODELS: dict[str, ModelProfile] = {
    m.name: m
    for m in (
        ModelProfile("opus", "claude", "claude-opus-5-5", description="Claude Opus via your login"),
        ModelProfile(
            "sonnet", "claude", "claude-sonnet-5-5", description="Claude Sonnet via your login"
        ),
        ModelProfile("sonnet-api", "anthropic", "claude-sonnet-5-5", description="API key"),
        ModelProfile("haiku-api", "anthropic", "claude-haiku-5-5", description="API key"),
        ModelProfile(
            "free",
            "openrouter",
            "openrouter/free",
            input_usd_per_mtok=0.0,
            output_usd_per_mtok=0.0,
            description="OpenRouter's free models (rate-limited; good for small tasks)",
        ),
        ModelProfile(
            "auto", "openrouter", "openrouter/auto", description="OpenRouter picks a model"
        ),
        ModelProfile("gpt", "openai", "gpt-6.1-sol", description="OpenAI GPT"),
    )
}

# Only agentline's own harness is built in (phase 44); the CLI harnesses are added by the user
# (Settings > Harnesses, the setup, the Chat), from these presets. An install that relied on a
# built-in claude-code / codex / aider got it written to agentline.toml once (harness_migration).
BUILTIN_HARNESSES: dict[str, HarnessProfile] = {
    "builtin": HarnessProfile(
        "builtin", "builtin", model="free", description="Works with just an API key"
    ),
}
HARNESS_PRESETS: dict[str, HarnessProfile] = {
    a.name: a
    for a in (
        HarnessProfile(
            "claude-code", "claude-code", description="Your Claude login, its own model"
        ),
        HarnessProfile("codex", "codex", model="gpt"),
        HarnessProfile("aider", "aider", model="sonnet-api"),
        HarnessProfile("opencode", "opencode", description="OpenCode, its own default model"),
        HarnessProfile("hermes-agent", "hermes", description="Hermes Agent, its own default model"),
    )
}
FALLBACK_HARNESS = "builtin"  # a node that names no harness and has no pipeline default
# Old harness names still accepted where a harness is named (`agent: llm` in older pipelines).
HARNESS_ALIASES = {"llm": "builtin"}


def removed_builtins(config: Config | None) -> set[str]:
    """Built-in sources the user removed (`[removed] providers`); a configured one wins."""
    if not config:
        return set()
    return {
        n for n in config.removed_providers if n in BUILTIN_PROVIDERS and n not in config.providers
    }


def providers(config: Config | None) -> dict[str, Provider]:
    gone = removed_builtins(config)
    out = {n: p for n, p in BUILTIN_PROVIDERS.items() if n not in gone}
    if config:
        out.update(config.providers)
    return out


def models(config: Config | None) -> dict[str, ModelProfile]:
    """Built-in models (minus those of removed built-in sources) plus configured ones."""
    gone = removed_builtins(config)
    out = {n: m for n, m in BUILTIN_MODELS.items() if m.provider not in gone}
    if config:
        out.update(config.models)
    return out


def harnesses(config: Config | None) -> dict[str, HarnessProfile]:
    """Built-in harness profiles (one per kind) plus configured ones; config wins by name. A
    built-in whose default model went with a removed source has no default model."""
    known = models(config)
    out = {
        n: h if h.model is None or h.model in known else replace(h, model=None)
        for n, h in BUILTIN_HARNESSES.items()
    }
    if config:
        out.update(config.harnesses)
    return out


def harness_name(ref: str | None, config: Config | None) -> str | None:
    """`ref` with an old alias (`llm`) mapped to its new name, unless a profile has that name."""
    if ref is None or ref in harnesses(config):
        return ref
    return HARNESS_ALIASES.get(ref, ref)


def kind_name(kind: str) -> str:
    return HARNESS_KIND_ALIASES.get(kind, kind)


@dataclass
class Resolution:
    harness: HarnessProfile
    kind: HarnessKind
    model: ModelProfile | None = None
    provider: Provider | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def runner(self) -> str:
        return self.kind.runner


def resolve_model(ref: str, config: Config | None, default_provider: str | None) -> ModelProfile:
    """A `[models]` name, `provider:model-id`, or a bare id on `default_provider`."""
    known = models(config)
    if ref in known:
        return known[ref]
    provs = providers(config)
    head, sep, rest = ref.partition(":")
    if sep and head in provs and rest:
        return ModelProfile(name=ref, provider=head, model=rest)
    if default_provider is None:
        names = ", ".join(sorted(known)) or "none defined"
        raise ValidationError(
            f"model {ref!r} is not a [models] name (known: {names}) and has no provider;"
            " write provider:model-id"
        )
    return ModelProfile(name=ref, provider=default_provider, model=ref)


def choose(
    node_harness: str | None,
    node_model: str | None,
    default_harness: str | None,
    default_model: str | None,
) -> tuple[str | None, str | None]:
    """A node's (harness, model) after pipeline defaults. Defaults are a pair: a node that picks
    a different harness gets that harness's own default model, not the pipeline's default."""
    harness = node_harness or default_harness
    if node_model is not None:
        return harness, node_model
    if node_harness is None or node_harness == default_harness:
        return harness, default_model
    return harness, None


def resolve(
    harness_ref: str | None,
    model_ref: str | None,
    config: Config | None,
    *,
    presets: bool = False,
) -> Resolution:
    """Turn a node's harness/model choice into a runner name and runner params. `presets`
    lets agentline's own uses (the vision transcriber) run a CLI harness that isn't set up."""
    profiles = harnesses(config)
    if presets:
        profiles = {**HARNESS_PRESETS, **profiles}
    name = harness_name(harness_ref, config) or ("builtin" if model_ref else FALLBACK_HARNESS)
    if name not in profiles:
        if name in HARNESS_PRESETS:
            raise ValidationError(
                f"harness {name!r} isn't set up: add it under Settings > Harnesses"
                " (or pick a configured one)"
            )
        known = ", ".join(sorted(profiles))
        raise ValidationError(f"unknown harness {name!r} (known: {known})")
    profile = profiles[name]
    kind = KINDS.get(kind_name(profile.kind))
    if kind is None:
        raise ValidationError(
            f"harness {name!r} has unknown kind {profile.kind!r} (kinds: {', '.join(KINDS)})"
        )
    ref = model_ref or profile.model
    res = Resolution(harness=profile, kind=kind)
    if ref is None:
        if kind.needs_model:
            raise ValidationError(f"harness {name!r} ({kind.name}) needs a model")
        if kind.name == "claude-code" and config and "anthropic" in config.providers:
            res.provider = config.providers["anthropic"]  # its auth decides which login is used
    else:
        default_provider = "anthropic" if kind.provider_types == ("anthropic",) else None
        res.model = resolve_model(ref, config, default_provider)
        provs = providers(config)
        if res.model.provider not in provs:
            raise ValidationError(
                f"model {ref!r} uses unknown provider {res.model.provider!r}"
                f" (known: {', '.join(sorted(provs))})"
            )
        res.provider = provs[res.model.provider]
        check_compatible(kind, res.provider, ref)
    res.params = _params(res)
    return res


def check_compatible(kind: HarnessKind, provider: Provider, model_ref: str) -> None:
    if provider.type not in kind.provider_types:
        hint = (
            " Claude Code uses Anthropic models only; use harness: builtin (or aider/codex)"
            " for OpenRouter or a private server."
            if kind.name == "claude-code"
            else ""
        )
        raise ValidationError(
            f"harness kind {kind.name!r} can't use model {model_ref!r} from {provider.type}"
            f" provider {provider.name!r} (it supports: {', '.join(kind.provider_types)}).{hint}"
        )
    if provider.auth == "subscription" and not kind.subscription:
        raise ValidationError(
            f"provider {provider.name!r} is a Claude subscription login, which only claude-code"
            f" can use; {kind.name!r} needs an API key provider"
        )


def _params(res: Resolution) -> dict[str, Any]:
    """Runner `with:` params for this harness + model (a node's own `with:` overrides them)."""
    p: dict[str, Any] = {}
    a, m, prov = res.harness, res.model, res.provider
    if a.bin:
        p["bin"] = a.bin
    if a.extra_args:
        p["extra_args"] = list(a.extra_args)
    env = list(a.env or ())
    k = res.kind.name
    if m is not None and m.priced:
        p["input_usd_per_mtok"] = m.input_usd_per_mtok
        p["output_usd_per_mtok"] = m.output_usd_per_mtok
    if k == "builtin":
        assert m is not None and prov is not None
        p.update(
            model=m.model,
            provider_type=prov.type,
            base_url=prov.base_url,
            api_key_env=prov.api_key_env,
            headers=dict(prov.headers),
        )
        if m.max_tokens is not None:
            p["max_tokens"] = m.max_tokens
        if m.temperature is not None:
            p["temperature"] = m.temperature
        if not m.priced:
            p["price_unknown_ok"] = prov.type == "openai-compatible" and m.model.endswith(":free")
        return p
    if k == "claude-code":
        if prov is not None:
            if prov.auth == "subscription":
                p["env"] = env  # no ANTHROPIC_API_KEY: the CLI uses its own login
            else:
                p["env"] = env
                p["env_map"] = {"ANTHROPIC_API_KEY": prov.api_key_env or "ANTHROPIC_API_KEY"}
            if prov.base_url:
                p["env_set"] = {"ANTHROPIC_BASE_URL": prov.base_url}
        elif env:
            p["env"] = ["ANTHROPIC_API_KEY", *env]
        if m is not None:
            p["model"] = m.model
        return p
    if k == "aider":
        if prov is not None and m is not None:
            if prov.type == "anthropic":
                p["model"] = f"anthropic/{m.model}"
                p["env_map"] = {"ANTHROPIC_API_KEY": prov.api_key_env or "ANTHROPIC_API_KEY"}
            else:
                p["model"] = f"openai/{m.model}"
                p["extra_args"] = [*p.get("extra_args", []), "--openai-api-base", prov.base_url]
                if prov.api_key_env:
                    p["env_map"] = {"OPENAI_API_KEY": prov.api_key_env}
            p["env"] = env
        elif env:
            p["env"] = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", *env]
        return p
    if k == "opencode":
        if prov is not None and m is not None:
            if prov.type == "anthropic":
                p["model"] = f"anthropic/{m.model}"
                p["env_map"] = {"ANTHROPIC_API_KEY": prov.api_key_env or "ANTHROPIC_API_KEY"}
                if prov.base_url:
                    p["env_set"] = {"ANTHROPIC_BASE_URL": prov.base_url}
            else:
                # the source as an OpenCode provider named `agentline`, declared in the
                # config OpenCode merges from OPENCODE_CONFIG_CONTENT (no key in it: {env:})
                options: dict[str, Any] = {"baseURL": prov.base_url}
                if prov.api_key_env:
                    options["apiKey"] = "{env:AGENTLINE_PROVIDER_KEY}"
                    p["env_map"] = {"AGENTLINE_PROVIDER_KEY": prov.api_key_env}
                if prov.headers:
                    options["headers"] = dict(prov.headers)
                provider = {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": prov.name,
                    "options": options,
                    "models": {m.model: {"name": m.name}},
                }
                p["model"] = f"agentline/{m.model}"
                p["env_set"] = {
                    "OPENCODE_CONFIG_CONTENT": json.dumps({"provider": {"agentline": provider}})
                }
            p["env"] = env
        elif env:
            p["env"] = [*opencode_default_env(), *env]
        return p
    if k == "hermes":
        if prov is not None and m is not None:
            p["model"] = m.model
            if prov.type == "anthropic":
                p["provider"] = "anthropic"
                p["env_map"] = {"ANTHROPIC_API_KEY": prov.api_key_env or "ANTHROPIC_API_KEY"}
                if prov.base_url:
                    p["env_set"] = {"ANTHROPIC_BASE_URL": prov.base_url}
            elif "openrouter.ai" in (prov.base_url or ""):
                p["provider"] = "openrouter"
                p["env_map"] = {"OPENROUTER_API_KEY": prov.api_key_env or "OPENROUTER_API_KEY"}
            else:
                p["provider"] = "custom"
                p["base_url"] = prov.base_url
                if prov.api_key_env:
                    p["env_map"] = {"OPENAI_API_KEY": prov.api_key_env}
                else:  # a keyless local server; the OpenAI client still wants a key
                    p["env_set"] = {"OPENAI_API_KEY": "none"}
            p["env"] = env
        elif env:
            p["env"] = [*opencode_default_env(), *env]
        return p
    if k == "codex":
        if prov is not None and m is not None:
            # A custom model provider through `-c` config overrides (Codex's config.toml keys).
            p["model"] = m.model
            p["extra_args"] = [
                *p.get("extra_args", []),
                "-c", "model_provider=agentline",
                "-c", 'model_providers.agentline.name="agentline"',
                "-c", f'model_providers.agentline.base_url="{prov.base_url}"',
                "-c", 'model_providers.agentline.env_key="AGENTLINE_PROVIDER_KEY"',
            ]  # fmt: skip
            if prov.api_key_env:
                p["env_map"] = {"AGENTLINE_PROVIDER_KEY": prov.api_key_env}
            p["env"] = env
        elif env:
            p["env"] = ["OPENAI_API_KEY", *env]
        return p
    return p


def opencode_default_env() -> list[str]:
    """The keys OpenCode and Hermes look for when a harness names no model (their own default)."""
    return ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"]


def summary(
    config: Config | None, ready: dict[str, tuple[bool, str]] | None = None
) -> dict[str, Any]:
    """Everything the GUI needs to render pickers and the Settings page (no secret values).
    With `ready` (readiness.ready_sources), sources and models say whether they can be used
    now, and `compatible_models` (the pickers) lists only usable ones."""
    provs = providers(config)
    ok = {n: (ready or {}).get(n, (True, "")) for n in provs}
    configured = set(config.providers) if config else set()
    return {
        "kinds": {
            k.name: {
                "runner": k.runner,
                "provider_types": list(k.provider_types),
                "needs_model": k.needs_model,
                "subscription": k.subscription,
                "default_bin": k.default_bin,
                "description": k.description,
            }
            for k in KINDS.values()
        },
        "providers": [
            {
                **_public(p),
                "implicit": p.name not in configured,
                "builtin": p.name in BUILTIN_PROVIDERS,
                "ready": ok[p.name][0],
                "not_ready": ok[p.name][1] or None,
                # the Models page: groups for connected sources; a source the user set up
                # that stopped working goes to "Needs attention"
                "connected": ok[p.name][0],
                "user_set": p.name in configured
                or any(m.provider == p.name for m in (config.models.values() if config else [])),
            }
            for p in provs.values()
        ],
        "removed_builtins": sorted(removed_builtins(config)),
        "models": [
            {
                **_public(m),
                "implicit": not (config and m.name in config.models),
                "ready": ok.get(m.provider, (False, ""))[0],
            }
            for m in models(config).values()
        ],
        "harnesses": [
            {
                **_public(a),
                "implicit": not (config and a.name in config.harnesses),
                # None: needs no program (builtin); else is its program on PATH
                "installed": _installed(a.bin or KINDS[kind_name(a.kind)].default_bin)
                if KINDS.get(kind_name(a.kind)) and KINDS[kind_name(a.kind)].default_bin
                else None,
            }
            for a in harnesses(config).values()
        ],
        # harnesses that can be added (not set up yet), for the "+ Add a harness" popups
        "presets": [
            {**_public(a), "installed": _installed(KINDS[a.kind].default_bin)}
            for a in HARNESS_PRESETS.values()
            if a.name not in harnesses(config)
        ],
        "compatible_models": {
            a.name: [
                m.name
                for m in models(config).values()
                if m.provider in provs
                and not m.hidden
                and ok[m.provider][0]
                and provs[m.provider].type in KINDS[kind_name(a.kind)].provider_types
                and (
                    provs[m.provider].auth != "subscription"
                    or KINDS[kind_name(a.kind)].subscription
                )
            ]
            for a in harnesses(config).values()
            if kind_name(a.kind) in KINDS
        },
    }


def _installed(binary: str | None) -> bool:
    import shutil

    return bool(binary and shutil.which(binary))


def _public(obj: Any) -> dict[str, Any]:
    d = dict(obj.__dict__)
    for k, v in list(d.items()):
        if isinstance(v, tuple):
            d[k] = list(v)
    return d
