"""Can a model source be used right now: its key is set (environment or secrets file), or, for
a Claude subscription, the Claude Code CLI is installed and logged in. Settings and the editor
hide the models of sources that aren't ready."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any

from agentline import agents as ai
from agentline.config import Config, Provider
from agentline.secrets import read_secrets

LOGIN_TTL = 30.0  # seconds a `claude auth status` answer is reused
_login_cache: dict[str, tuple[float, dict[str, Any]]] = {}
SERVER_TTL = 15.0  # seconds a local server's answer is reused
_server_cache: dict[str, tuple[float, bool]] = {}


def server_up(base_url: str | None) -> bool:
    """Does a keyless local server (Ollama, LM Studio) answer `GET <base>/models` (cached)?"""
    import urllib.error
    import urllib.request

    if not base_url:
        return False
    hit = _server_cache.get(base_url)
    if hit and time.monotonic() - hit[0] < SERVER_TTL:
        return hit[1]
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=1.5) as r:
            up = 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        up = False
    _server_cache[base_url] = (time.monotonic(), up)
    return up


def claude_bin(cfg: Config | None) -> dict[str, Any]:
    """Where the claude-code harness's CLI is (`path` None when it isn't installed / on PATH)."""
    profile = ai.harnesses(cfg).get("claude-code")
    binary = (profile.bin if profile else None) or "claude"
    return {"bin": binary, "path": shutil.which(binary)}


def claude_login(cfg: Config | None, fresh: bool = False) -> dict[str, Any]:
    """`claude auth status`: {cli, logged_in, method, subscription, email} (cached briefly)."""
    cli = claude_bin(cfg)["path"]
    if cli is None:
        return {"cli": None, "logged_in": False}
    hit = _login_cache.get(cli)
    if hit and not fresh and time.monotonic() - hit[0] < LOGIN_TTL:
        return hit[1]
    try:
        proc = subprocess.run(
            [cli, "auth", "status", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        status = json.loads(proc.stdout)
        out = {
            "cli": cli,
            "logged_in": bool(status.get("loggedIn")),
            "method": status.get("authMethod"),
            "subscription": status.get("subscriptionType"),
            "email": status.get("email"),
        }
    except (OSError, ValueError, subprocess.TimeoutExpired, AttributeError) as e:
        out = {"cli": cli, "logged_in": False, "error": str(e)[:500]}
    _login_cache[cli] = (time.monotonic(), out)
    return out


def forget_login() -> None:
    _login_cache.clear()


def provider_ready(p: Provider, cfg: Config | None) -> tuple[bool, str]:
    """(ready, why not)."""
    if p.auth == "subscription":
        login = claude_login(cfg)
        if login["cli"] is None:
            return False, "Claude Code isn't installed"
        return (True, "") if login["logged_in"] else (False, "Claude Code is signed out")
    if p.api_key_env:
        if os.environ.get(p.api_key_env) or read_secrets().get(p.api_key_env):
            return True, ""
        return False, f"{p.api_key_env} isn't set"
    if p.name in ai.BUILTIN_PROVIDERS and not (cfg and p.name in cfg.providers):
        # a built-in local server (Ollama): only usable while it answers
        return (True, "") if server_up(p.base_url) else (False, f"{p.name} isn't running")
    return True, ""  # a keyless server the user set up (a private LLM): assume it's there


def ready_sources(cfg: Config | None) -> dict[str, tuple[bool, str]]:
    return {name: provider_ready(p, cfg) for name, p in ai.providers(cfg).items()}
