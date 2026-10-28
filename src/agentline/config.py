"""agentline.toml: repos, environments (deploy/test commands), images, and the AI setup:
providers (where models come from), named models, and agent profiles. Local to the agentline
host, never read from a target repo (decisions 5 and 9). Secrets are never stored here: a
provider names the env var that holds its key (see `agentline.secrets`)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError


@dataclass(frozen=True)
class Environment:
    name: str
    deploy: str
    test: str | None = None
    deploy_class: bool = True


@dataclass(frozen=True)
class RepoConfig:
    name: str
    remote: str
    image: str | None = None
    image_profile: str | None = None
    default_branch: str = "main"


PROVIDER_TYPES = ("anthropic", "openai-compatible")
ANTHROPIC_AUTH = ("api-key", "subscription")


@dataclass(frozen=True)
class Provider:
    """Where models come from. `anthropic` (API key or a Claude subscription login, which only
    the claude-code agent can use) or `openai-compatible` (OpenRouter, OpenAI, Ollama, vLLM,
    LM Studio, any private server that speaks the OpenAI chat API)."""

    name: str
    type: str
    base_url: str | None = None
    api_key_env: str | None = None  # env var holding the key (None: no key, e.g. local server)
    auth: str = "api-key"  # anthropic only: api-key | subscription
    headers: dict[str, str] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class ModelProfile:
    """A named model: `[models.cheap] provider = "openrouter", model = "..."`."""

    name: str
    provider: str
    model: str
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    description: str = ""
    hidden: bool = False  # set up but kept out of every picker (Settings > Models, the eye)

    @property
    def priced(self) -> bool:
        return self.input_usd_per_mtok is not None and self.output_usd_per_mtok is not None


@dataclass(frozen=True)
class HarnessProfile:
    """A harness (the CLI or loop that drives a model: Claude Code, Codex, Aider, or the built-in
    one) and its settings: `[harnesses.claude-code] kind = "claude-code"`. `model` is the
    default model (a `[models]` name or `provider:model-id`); None means the harness's own
    default."""

    name: str
    kind: str
    bin: str | None = None
    model: str | None = None
    extra_args: tuple[str, ...] = ()
    env: tuple[str, ...] | None = None  # extra host env var names to pass through
    description: str = ""


def _str(table: dict[str, Any], key: str, where: str, required: bool = False) -> str | None:
    v = table.get(key)
    if v is None:
        if required:
            raise ValidationError(f"{where} needs {key!r}")
        return None
    if not isinstance(v, str) or not v:
        raise ValidationError(f"{where}.{key} must be a non-empty string")
    return v


def _num(table: dict[str, Any], key: str, where: str) -> float | None:
    v = table.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ValidationError(f"{where}.{key} must be a non-negative number")
    return float(v)


def parse_provider(name: str, t: dict[str, Any]) -> Provider:
    where = f"[providers.{name}]"
    ptype = _str(t, "type", where, required=True)
    if ptype not in PROVIDER_TYPES:
        raise ValidationError(f"{where}.type must be one of {', '.join(PROVIDER_TYPES)}")
    base_url = _str(t, "base_url", where)
    if ptype == "openai-compatible" and not base_url:
        raise ValidationError(f"{where} needs 'base_url' (e.g. https://openrouter.ai/api/v1)")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValidationError(f"{where}.base_url must start with http:// or https://")
    auth = _str(t, "auth", where) or "api-key"
    if auth not in ANTHROPIC_AUTH or (ptype != "anthropic" and auth != "api-key"):
        raise ValidationError(
            f"{where}.auth must be api-key or subscription (subscription: anthropic only)"
        )
    key_env = _str(t, "api_key_env", where)
    if key_env and not key_env.replace("_", "").isalnum():
        raise ValidationError(f"{where}.api_key_env must be an env var name, not a key")
    headers = t.get("headers") or {}
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        raise ValidationError(f"{where}.headers must be a table of strings")
    return Provider(
        name=name,
        type=ptype,
        base_url=base_url.rstrip("/") if base_url else None,
        api_key_env=key_env,
        auth=auth,
        headers=dict(headers),
        description=str(t.get("description", "")),
    )


def parse_model(name: str, t: dict[str, Any]) -> ModelProfile:
    where = f"[models.{name}]"
    max_tokens = t.get("max_tokens")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)):
        raise ValidationError(f"{where}.max_tokens must be an integer")
    return ModelProfile(
        name=name,
        provider=str(_str(t, "provider", where, required=True)),
        model=str(_str(t, "model", where, required=True)),
        input_usd_per_mtok=_num(t, "input_usd_per_mtok", where),
        output_usd_per_mtok=_num(t, "output_usd_per_mtok", where),
        max_tokens=max_tokens,
        temperature=_num(t, "temperature", where),
        description=str(t.get("description", "")),
        hidden=bool(t.get("hidden", False)),
    )


# Old names still read from user files: `[agents.*]` tables and `kind = "llm"`.
HARNESS_KIND_ALIASES = {"llm": "builtin"}


def parse_harness(name: str, t: dict[str, Any]) -> HarnessProfile:
    where = f"[harnesses.{name}]"
    for key in ("extra_args", "env"):
        v = t.get(key)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
            raise ValidationError(f"{where}.{key} must be a list of strings")
    env = t.get("env")
    kind = str(_str(t, "kind", where) or name)
    return HarnessProfile(
        name=name,
        kind=HARNESS_KIND_ALIASES.get(kind, kind),
        bin=_str(t, "bin", where),
        model=_str(t, "model", where),
        extra_args=tuple(t.get("extra_args") or ()),
        env=tuple(env) if env is not None else None,
        description=str(t.get("description", "")),
    )


@dataclass
class Config:
    path: Path | None = None
    repos: dict[str, RepoConfig] = field(default_factory=dict)
    environments: dict[str, Environment] = field(default_factory=dict)
    images: dict[str, dict[str, Any]] = field(default_factory=dict)
    allow_images: tuple[str, ...] = ()
    providers: dict[str, Provider] = field(default_factory=dict)
    models: dict[str, ModelProfile] = field(default_factory=dict)
    harnesses: dict[str, HarnessProfile] = field(default_factory=dict)
    mcp: dict[str, Any] = field(default_factory=dict)  # [mcp.*]: agentline.mcp.McpServer
    settings: dict[str, Any] = field(default_factory=dict)  # [settings]: advisor/summary model
    removed_providers: tuple[str, ...] = ()  # [removed] providers: built-in sources taken out
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """Load from `path`, $AGENTLINE_CONFIG, or ./agentline.toml; empty if none exists."""
        candidate = path or os.environ.get("AGENTLINE_CONFIG") or "agentline.toml"
        p = Path(candidate)
        if not p.exists():
            if path is not None:
                raise ValidationError(f"config file {p} not found")
            return cls()
        with p.open("rb") as f:
            raw = tomllib.load(f)
        return cls.from_dict(raw, p)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], path: Path | None = None) -> Config:
        cfg = cls(path=path, raw=raw)
        for name, r in raw.get("repos", {}).items():
            if "remote" not in r:
                raise ValidationError(f"[repos.{name}] needs 'remote'")
            cfg.repos[name] = RepoConfig(
                name=name,
                remote=r["remote"],
                image=r.get("image"),
                image_profile=r.get("image_profile"),
                default_branch=r.get("default_branch", "main"),
            )
        for name, e in raw.get("environments", {}).items():
            if "deploy" not in e:
                raise ValidationError(f"[environments.{name}] needs 'deploy'")
            cfg.environments[name] = Environment(
                name=name,
                deploy=e["deploy"],
                test=e.get("test"),
                deploy_class=bool(e.get("deploy_class", True)),
            )
        cfg.images = dict(raw.get("images", {}))
        cfg.allow_images = tuple(raw.get("allow_images", ()))
        for name, t in raw.get("providers", {}).items():
            cfg.providers[name] = parse_provider(name, t)
        for name, t in raw.get("models", {}).items():
            cfg.models[name] = parse_model(name, t)
        old = raw.get("agents", {})  # the pre-phase-35 name of [harnesses]
        for name in set(old) & set(raw.get("harnesses", {})):
            raise ValidationError(f"{name!r} is in both [harnesses] and [agents]: keep one")
        for name, t in {**old, **raw.get("harnesses", {})}.items():
            cfg.harnesses[name] = parse_harness(name, t)
        from agentline.mcp import parse_mcp

        for name, t in raw.get("mcp", {}).items():
            cfg.mcp[name] = parse_mcp(name, t)
        cfg.settings = dict(raw.get("settings", {}))
        gone = raw.get("removed", {}).get("providers", [])
        if not isinstance(gone, list) or not all(isinstance(x, str) for x in gone):
            raise ValidationError("[removed].providers must be a list of source names")
        cfg.removed_providers = tuple(gone)
        return cfg

    def environment(self, name: str) -> Environment:
        if name not in self.environments:
            known = ", ".join(sorted(self.environments)) or "none configured"
            raise ValidationError(f"unknown environment {name!r} (known: {known})")
        return self.environments[name]
