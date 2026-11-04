"""First-run setup, in two steps: 1. model sources (an API key, a Claude login, a local
server), 2. the harness the Chat uses (Claude Code, Codex, Aider, or the built-in one that needs
only a key) and its model.

`sources` are the model sources (agents.providers) and whether each can be used now; `harnesses`
are the harness profiles and the models each can use with what is set up. The user's pick is
kept in `$AGENTLINE_HOME/setup.json` (`{"assistant": {"harness", "model"}, "dismissed"}`; before
phase 35 `"agent"`, still read) and is the Chat's default. The GUI shows the setup popup while
`needs_setup` is true: on the first run, and whenever no harness can run.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from agentline import agents as ai
from agentline.config import Config
from agentline.errors import ValidationError

# Step 1: the order and wording of the model sources everyone has (agents.BUILTIN_PROVIDERS).
SOURCE_TEXT: dict[str, tuple[str, str]] = {
    "openrouter": ("OpenRouter", "One key, hundreds of models; the free ones cost nothing."),
    "anthropic": ("Anthropic API", "Claude models, billed per token."),
    "openai": ("OpenAI API", "GPT models, billed per token."),
    "claude": ("Claude subscription", "Your Pro/Max login, through the Claude Code CLI."),
    "ollama": ("Ollama on this computer", "Local models, no key. Start Ollama, pull a model."),
}
# Step 2: the harness picked by default, best first, and the model each prefers.
HARNESS_ORDER = ("claude-code", "builtin", "codex", "aider", "opencode", "hermes-agent")
PREFERRED_MODELS = ("free", "sonnet-api", "gpt", "haiku-api", "auto")
HARNESS_TEXT: dict[str, tuple[str, str]] = {  # shown as plain text: no markdown
    "builtin": (
        "Built-in",
        "agentline's own harness: works with just an API key, nothing to install.",
    ),
    "claude-code": (
        "Claude Code",
        "The claude program, with your Claude login. Best for building pipelines.",
    ),
    "codex": ("Codex", "OpenAI's Codex CLI (the codex program)."),
    "aider": ("Aider", "The aider program."),
    "opencode": ("OpenCode", "The opencode program, with its own or a connected model."),
    "hermes-agent": ("Hermes Agent", "Nous Research's hermes program."),
}


def setup_path() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "setup.json"


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(setup_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    choice = data.get("assistant")
    if isinstance(choice, dict) and "agent" in choice:  # saved before phase 35
        old = choice.pop("agent")
        choice.setdefault("harness", ai.HARNESS_ALIASES.get(old, old) if old else old)
    return data


def write_state(state: dict[str, Any]) -> None:
    path = setup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".setup.json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _local_models(provider: Any) -> list[str]:
    """Models a keyless local server (Ollama, LM Studio...) has, as `<source>:<id>` (quick)."""
    from agentline.api.settings import list_models

    try:
        found = list_models(provider, timeout=1.5)
    except Exception:  # not running, or not answering like an OpenAI-compatible server
        return []
    return [f"{provider.name}:{m['id']}" for m in found]


def _claude_cli(cfg: Config | None) -> str | None:
    profile = ai.harnesses(cfg).get("claude-code")
    return shutil.which((profile.bin if profile else None) or "claude")


def source_status(provider: Any, cfg: Config | None, check_local: bool) -> dict[str, Any]:
    """Step 1: can this model source be used now, and what it takes if not."""
    title, desc = SOURCE_TEXT.get(provider.name, (provider.name, provider.description))
    out: dict[str, Any] = {
        "id": provider.name,
        "title": title,
        "description": desc or provider.description,
        "type": provider.type,
        "key": provider.api_key_env,
        "local_models": [],
    }
    if provider.auth == "subscription":
        found = _claude_cli(cfg)
        out["available"] = bool(found)
        out["hint"] = (
            f"found {found}; if it isn't logged in yet, run claude once in Terminal"
            if found
            else "Install Claude Code (npm install -g @anthropic-ai/claude-code), run claude"
            " once to log in, then Check again"
        )
    elif provider.api_key_env:
        out["available"] = bool(os.environ.get(provider.api_key_env))
        out["hint"] = "key set" if out["available"] else f"paste your {provider.api_key_env}"
    else:  # a keyless server: usable when it answers
        models = _local_models(provider) if check_local else []
        out["local_models"] = models
        out["available"] = bool(models)
        out["hint"] = (
            f"{len(models)} model(s), e.g. {models[0].split(':', 1)[1]}"
            if models
            else "not running, or no models yet"
        )
    return out


def harness_status(
    profile: Any, cfg: Config | None, sources: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Step 2: can this harness run now, and with which models (only ones whose source works)."""
    import dataclasses

    kind = ai.KINDS[ai.kind_name(profile.kind)]
    every = ai.models(cfg)
    provs = ai.providers(cfg)
    configured = profile.name in ai.harnesses(cfg)
    if not configured:  # a preset not set up yet: its models as if it were
        base = cfg or Config()
        cfg = dataclasses.replace(base, harnesses={**base.harnesses, profile.name: profile})
    models = [
        m
        for m in ai.summary(cfg)["compatible_models"].get(profile.name, [])
        if sources.get(every[m].provider, {}).get("available")
    ]
    for src in sources.values():  # models found on running local servers
        prov = provs.get(src["id"])
        if prov is not None and prov.type in kind.provider_types:
            models += [m for m in src["local_models"] if m not in models]
    models.sort(key=lambda m: PREFERRED_MODELS.index(m) if m in PREFERRED_MODELS else 99)
    text = HARNESS_TEXT.get(profile.name)
    out: dict[str, Any] = {
        "id": profile.name,
        "kind": kind.name,
        "title": text[0] if text else profile.name,
        "description": text[1] if text else profile.description or kind.description,
        "models": models,
        "model": None,
        "cli": kind.default_bin,
        # False: a preset (Claude Code, Codex, Aider) that "Use for the Chat" adds first
        "configured": configured,
    }
    if kind.default_bin:
        binary = profile.bin or kind.default_bin
        found = shutil.which(binary)
        out["available"] = bool(found) and (not kind.needs_model or bool(models))
        out["hint"] = f"found {found}" if found else f"{binary} isn't installed (or not on PATH)"
        if kind.name == "claude-code" and found:
            out["hint"] += "; uses your Claude login (or an Anthropic key)"
        if found and not configured:
            out["hint"] += "; not set up yet: using it adds it to Settings > Harnesses"
        if found and profile.model in models:
            out["model"] = profile.model
    else:
        out["available"] = bool(models)
        out["model"] = models[0] if models else None
        out["hint"] = (
            f"{len(models)} model(s) ready" if models else "needs a model source from step 1"
        )
    return out


def status(cfg: Config | None, *, check_local: bool = True) -> dict[str, Any]:
    state = read_state()
    provs = ai.providers(cfg)
    order = [n for n in SOURCE_TEXT if n in provs] + [n for n in provs if n not in SOURCE_TEXT]
    sources = [source_status(provs[n], cfg, check_local) for n in order]
    by_id = {x["id"]: x for x in sources}
    profiles = ai.harnesses(cfg)
    names = [n for n in HARNESS_ORDER if n in profiles]
    names += [n for n in profiles if n not in HARNESS_ORDER]
    harnesses = [
        harness_status(profiles[n], cfg, by_id)
        for n in names
        if ai.kind_name(profiles[n].kind) in ai.KINDS
    ]
    # CLI harnesses that aren't set up: offered when their program is installed
    for p in ai.HARNESS_PRESETS.values():
        if p.name not in profiles and shutil.which(ai.KINDS[p.kind].default_bin or ""):
            harnesses.append(harness_status(p, cfg, by_id))
    order = {n: i for i, n in enumerate(HARNESS_ORDER)}
    harnesses.sort(key=lambda h: (not h["configured"], order.get(h["id"], 99)))
    ready = any(h["available"] for h in harnesses)
    choice = state.get("assistant") if isinstance(state.get("assistant"), dict) else None
    chosen_ok = bool(choice) and any(
        h["available"] and h["id"] == choice.get("harness") for h in harnesses
    )
    suggested = (
        choice
        if chosen_ok
        else next(
            ({"harness": h["id"], "model": h["model"]} for h in harnesses if h["available"]),
            None,
        )
    )
    return {
        "ready": ready,
        "first_run": not state,
        "needs_setup": not ready or not state,
        "choice": choice,
        "assistant": suggested,
        "sources": sources,
        "harnesses": harnesses,
        "managed": os.environ.get("AGENTLINE_KEYS_MANAGED") or None,
    }


def choose(cfg: Config | None, harness: str, model: str | None) -> dict[str, Any]:
    """Save the Chat's harness (and model); it must resolve (a preset that isn't set up is
    added by the API first: api/setup.py)."""
    harness = ai.harness_name(harness, cfg) or harness
    ai.resolve(harness, model, cfg)  # raises ValidationError when it can't
    state = read_state()
    state["assistant"] = {"harness": harness, "model": model}
    state["chosen_at"] = time.time()
    write_state(state)
    return state


def dismiss() -> None:
    state = read_state()
    state["dismissed"] = time.time()
    write_state(state)


__all__ = ["ValidationError", "choose", "dismiss", "read_state", "status"]
