"""Settings API: harnesses, model providers and named models in the local agentline.toml, plus
API keys in the local secrets file. Mounted under `/api` by `create_server`.

    GET    /settings                          harnesses, providers, models, kinds, key status
    PUT    /settings/{section}/{name}         section: providers | models | harnesses | mcp
    DELETE /settings/{section}/{name}         ?with_models=true (providers): also delete the
                                              source's models; a built-in source goes to
                                              `[removed] providers`
    PUT    /secrets/{name}   {"value": ...}   write-only: values are never returned
    DELETE /secrets/{name}
    POST   /providers/{name}/test             list the provider's models (checks URL + key);
                                              on failure `problem` says what to fix
    GET    /providers/{name}/login            a subscription source's Claude Code login
    POST   /providers/{name}/logout           sign Claude Code out (`claude auth logout`)
    PUT    /providers/{name}/models {"models": [ids], "add_only"?}   the source's chosen
                                              models: adds the missing ones, removes the
                                              unchecked ones (none with add_only)
    POST   /harnesses/{name}/detect           run `<bin> --version`
    POST   /mcp/{name}/test                   connect, initialize, list the tools
    POST   /mcp/test {name, body, secrets}    test a server that isn't saved yet (`secrets`
                                              are typed key values, used for this test only)
    GET    /mcp/detect                        MCP servers in other tools' configs (Claude
                                              Code, Claude Desktop, Codex, Cursor, .mcp.json)
    POST   /mcp/import {items: [{id, name?}]} add detected servers; their secret values go to
                                              the secrets file, agentline.toml gets ${KEY}

agentline.toml is edited table by table (`agentline.tomlwrite`), so comments and everything
else in the file stay as they are. Workers that are already running keep the config they
started with: changes apply to new workers (the response says so).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentline import agents as ai
from agentline import mcp_detect, restart
from agentline.builtins import default_registry
from agentline.config import Config, parse_harness, parse_model, parse_provider
from agentline.errors import ValidationError
from agentline.mcp import parse_mcp, public, test_server, used_by
from agentline.readiness import claude_bin, claude_login, forget_login, ready_sources
from agentline.secrets import delete_secret, read_secrets, secret_status, set_secret
from agentline.tomlwrite import delete_table, set_plain_table, set_table, write_file

SECTIONS = {
    "providers": parse_provider,
    "models": parse_model,
    "harnesses": parse_harness,
    "mcp": parse_mcp,
}
OLD_SECTIONS = {"agents": "harnesses"}  # pre-phase-35 names still accepted in the API
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
RESTART_NOTE = "Saved. Running workers keep their old settings until restarted."


def saved_note(files: Any, *extra: str | None) -> str | None:
    """The banner after a save: silent when the engine reloads live (agentline.live), which is
    the normal case; the restart reminder only where it is still true. `extra` is what the
    user needs to know either way (e.g. a model that was kept)."""
    parts = [x for x in extra if x]
    if getattr(files, "live", None) is None:
        parts.append(RESTART_NOTE)
    return " ".join(parts) or None


class SecretBody(BaseModel):
    value: str


class McpDraft(BaseModel):
    name: str = "draft"
    body: dict[str, Any]
    secrets: dict[str, str] = {}


class McpImport(BaseModel):
    items: list[dict[str, str]]


def config_path(config: Config | None) -> Path:
    if config is not None and config.path is not None:
        return Path(config.path)
    return Path(os.environ.get("AGENTLINE_CONFIG") or "agentline.toml").resolve()


# fields GET /settings adds to entries; never written back to agentline.toml
VIEW_ONLY = {"name", "implicit", "builtin", "ready", "not_ready"}


def _clean(body: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so the TOML stays minimal (empty string / list / map = unset)."""
    out: dict[str, Any] = {}
    for k, v in body.items():
        if k in VIEW_ONLY or v is None or v == "" or v == [] or v == {}:
            continue
        out[k] = v
    return out


def _http_get(url: str, headers: dict[str, str], timeout: float = 15) -> Any:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# A Claude subscription has no model list API: Claude Code takes these aliases and ids.
SUBSCRIPTION_MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
    ("claude-fable-5-1", "Claude Fable 5.1"),
    ("opus", "Opus (latest, alias)"),
    ("sonnet", "Sonnet (latest, alias)"),
    ("haiku", "Haiku (latest, alias)"),
]


class ProviderProblem(Exception):
    """A model listing failed for a reason the user can fix: `problem` names it for the UI
    (no_key, auth, unreachable, http, bad_response) and `key` is the env var involved."""

    def __init__(self, problem: str, message: str, key: str | None = None) -> None:
        super().__init__(message)
        self.problem = problem
        self.key = key


def list_models(provider: Any, timeout: float = 15) -> list[dict[str, Any]]:
    """The provider's models: id, name, free (when the provider says so), context length."""
    if provider.type == "anthropic":
        if provider.auth == "subscription":
            return [
                {"id": i, "name": n, "free": False, "context_length": None}
                for i, n in SUBSCRIPTION_MODELS
            ]
        env = provider.api_key_env or "ANTHROPIC_API_KEY"
        key = os.environ.get(env)
        if not key:
            msg = f"{env} is not set (add it under Settings > Models)"
            raise ProviderProblem("no_key", msg, env)
        base = provider.base_url or "https://api.anthropic.com"
        data = _http_get(
            f"{base}/v1/models?limit=1000",
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout,
        )
    else:
        headers = dict(provider.headers)
        if provider.api_key_env:
            key = os.environ.get(provider.api_key_env)
            if not key:
                raise ProviderProblem(
                    "no_key",
                    f"{provider.api_key_env} is not set (add it under Settings > Models)",
                    provider.api_key_env,
                )
            headers["authorization"] = f"Bearer {key}"
        data = _http_get(f"{provider.base_url}/models", headers, timeout)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    else:
        raise ProviderProblem("bad_response", f"{provider.name} didn't return a model list")
    out = []
    for m in items:
        if not isinstance(m, dict) or "id" not in m:
            continue
        pricing = m.get("pricing") or {}
        free = str(m["id"]).endswith(":free") or (
            bool(pricing)
            and str(pricing.get("prompt")) in ("0", "0.0")
            and str(pricing.get("completion")) in ("0", "0.0")
        )
        out.append(
            {
                "id": m["id"],
                "name": m.get("name") or m.get("display_name") or m["id"],
                "free": free,
                "context_length": m.get("context_length"),
            }
        )
    return sorted(out, key=lambda x: (not x["free"], str(x["id"])))


def provider_failure(provider: Any, e: BaseException) -> dict[str, Any]:
    """A failed test as {ok: false, error, problem, key?}: what went wrong, in a form the app
    can turn into the fix (set a key, check the URL, start the server)."""
    if isinstance(e, ProviderProblem):
        out = {"ok": False, "error": str(e), "problem": e.problem, "models": []}
        if e.key:
            out["key"] = e.key
        return out
    if isinstance(e, urllib.error.HTTPError):
        auth = e.code in (401, 403)
        out = {
            "ok": False,
            "error": f"HTTP {e.code} from {provider.name}"
            + (": the key was refused" if auth else ""),
            "problem": "auth" if auth else "http",
            "models": [],
        }
        if auth and provider.api_key_env:
            out["key"] = provider.api_key_env
        return out
    if isinstance(e, (urllib.error.URLError, TimeoutError, OSError)):
        return {
            "ok": False,
            "error": f"can't reach {provider.base_url}: {e}",
            "problem": "unreachable",
            "models": [],
        }
    return {"ok": False, "error": str(e), "problem": "bad_response", "models": []}


def model_name_for(model_id: str, taken: set[str]) -> str:
    """A config name for a model id: `meta-llama/llama-3.3-70b:free` -> `llama-3.3-70b-free`,
    made unique against `taken`."""
    base = model_id.rsplit("/", 1)[-1].replace(":", "-")
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-.") or "model"
    if not _NAME.match(base):
        base = "m-" + base
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


class Visibility(BaseModel):
    hidden: bool


_MODEL_FIELDS = ("provider", "model", "input_usd_per_mtok", "output_usd_per_mtok", "max_tokens",
                 "temperature", "description")  # fmt: skip


def _builtin_fields(m: Any) -> dict[str, Any]:
    return {k: getattr(m, k) for k in _MODEL_FIELDS if getattr(m, k) not in (None, "")}


def visibility_table(cfg: Config, name: str, hidden: bool) -> dict[str, Any] | None:
    """The `[models.<name>]` table after hiding / showing it in pickers; None = no table (a
    built-in model shown again with nothing else changed). KeyError: no such model."""
    if name not in ai.models(cfg):
        raise KeyError(name)
    builtin = ai.BUILTIN_MODELS.get(name)
    raw = (cfg.raw.get("models") or {}).get(name)
    if raw is None:  # a built-in model: hiding it writes an override table with its fields
        raw = _builtin_fields(builtin)
    table = {k: v for k, v in raw.items() if k != "hidden"}
    if hidden:
        return {**table, "hidden": True}
    if builtin is not None and table == _builtin_fields(builtin):
        return None
    return table


class ModelChoice(BaseModel):
    models: list[str]
    add_only: bool = False  # add the missing ids, remove nothing (the source's model checklist)


def set_removed(text: str, names: list[str]) -> str:
    """agentline.toml with `[removed] providers = names` (the table dropped when empty)."""
    names = sorted(set(names))
    return set_plain_table(text, "removed", {"providers": names or None})


async def _run(*argv: str, timeout: float = 20) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise HTTPException(504, f"{argv[0]} {' '.join(argv[1:])} timed out") from None
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def create_settings_router(files: Any) -> APIRouter:
    """`files` is the editor's PipelineFiles: its config and registry are refreshed on save, so
    validation in the editor sees new harnesses and models right away."""
    router = APIRouter()

    def config() -> Config:
        return files.config or Config()

    def reload(path: Path) -> None:
        files.config = Config.load(path)
        files.registry = default_registry(files.config)

    def key_names(cfg: Config) -> list[str]:
        names = {p.api_key_env for p in ai.providers(cfg).values() if p.api_key_env}
        names |= {k for m in cfg.mcp.values() for k in m.key_names()}
        from agentline.chatlink.platforms import PLATFORMS

        names |= {k for p in PLATFORMS.values() for k in p.keys}  # Settings > Chat
        return sorted(names | set(read_secrets()))

    def key_owner(cfg: Config, name: str) -> str | None:
        """Which feature sets this key (its own page has the field): `provider:<name>`,
        `mcp:<name>`, `chat:<platform>`, `github`; None for the rest (General > Other keys)."""
        from agentline.chatlink.platforms import PLATFORMS

        for p in ai.providers(cfg).values():
            if p.api_key_env == name:
                return f"provider:{p.name}"
        for m in cfg.mcp.values():
            if name in m.key_names():
                return f"mcp:{m.name}"
        for plat, spec in PLATFORMS.items():
            if name in spec.keys:
                return f"chat:{plat}"
        return "github" if name == "GITHUB_TOKEN" else None

    def view() -> dict[str, Any]:
        cfg = config()
        out = ai.summary(cfg, ready_sources(cfg))
        users = used_by(files)
        out["mcp"] = [{**public(m), "used_by": users.get(m.name, [])} for m in cfg.mcp.values()]
        out["keys"] = [
            {**k, "owner": key_owner(cfg, k["name"])} for k in secret_status(key_names(cfg))
        ]
        out["config_path"] = str(config_path(cfg))
        out["keys_managed"] = os.environ.get("AGENTLINE_KEYS_MANAGED") or None
        return out

    @router.get("/settings")
    def get_settings() -> dict[str, Any]:
        return view()

    @router.put("/settings/{section}/{name}")
    def put_entry(section: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        section = OLD_SECTIONS.get(section, section)
        if section not in SECTIONS:
            raise HTTPException(404, f"unknown section {section!r}")
        if not _NAME.match(name):
            raise HTTPException(422, f"bad name {name!r}: letters, digits, _ . -")
        table = _clean(body)
        if "api_key" in table or "key" in table:
            raise HTTPException(422, "keys go in /api/secrets, never in agentline.toml")
        try:
            SECTIONS[section](name, table)  # same checks as loading the file
            path = config_path(files.config)
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            new = set_table(text, section, name, table)
            removed = config().removed_providers
            if section == "providers" and name in removed:  # added back (a preset)
                new = set_removed(new, [n for n in removed if n != name])
            cfg = Config.from_dict(tomllib.loads(new), path)
            if section == "models" and table.get("provider") not in ai.providers(cfg):
                raise ValidationError(f"model uses unknown provider {table.get('provider')!r}")
            if section == "harnesses":
                kind = ai.kind_name(table.get("kind", name))
                if kind not in ai.KINDS:
                    raise ValidationError(f"unknown harness kind {kind!r} ({', '.join(ai.KINDS)})")
                if table.get("model"):
                    ai.resolve(name, None, cfg)  # the default model must suit the harness
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        write_file(path, new)
        reload(path)
        restart.note(f"{section}.{name} changed")
        return {**view(), "note": saved_note(files)}

    @router.put("/settings/models/{name}/visibility")
    def put_visibility(name: str, body: Visibility) -> dict[str, Any]:
        """Hide a model from every picker (or show it again); it stays set up."""
        cfg = config()
        try:
            table = visibility_table(cfg, name, body.hidden)
        except KeyError:
            raise HTTPException(404, f"unknown model {name!r}") from None
        path = config_path(files.config)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            new = (
                delete_table(text, "models", name)
                if table is None
                else set_table(text, "models", name, table)
            )
            Config.from_dict(tomllib.loads(new), path)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        if new != text:
            write_file(path, new)
            reload(path)
            restart.note(f"models.{name} visibility")
        return {**view(), "note": saved_note(files)}

    @router.delete("/settings/{section}/{name}")
    def delete_entry(section: str, name: str, with_models: bool = False) -> dict[str, Any]:
        section = OLD_SECTIONS.get(section, section)
        if section not in SECTIONS:
            raise HTTPException(404, f"unknown section {section!r}")
        cfg = config()
        if section == "providers":
            return delete_source(cfg, name, with_models)
        configured = getattr(cfg, section)
        builtin = {
            "providers": ai.BUILTIN_PROVIDERS,
            "models": ai.BUILTIN_MODELS,
            "harnesses": ai.BUILTIN_HARNESSES,
            "mcp": {},
        }[section]
        if name not in configured:
            if name in builtin:
                raise HTTPException(
                    409, f"{name!r} is built in and can't be deleted; edit it to override it"
                )
            raise HTTPException(404, f"no {section[:-1]} {name!r}")
        users: list[str] = []
        if section == "models":
            users = [a.name for a in cfg.harnesses.values() if a.model == name]
            if users:
                raise HTTPException(
                    409, f"model {name!r} is the default of harnesses: {', '.join(users)}"
                )
        path = config_path(files.config)
        if not path.exists():
            raise HTTPException(404, f"{path} doesn't exist")
        try:
            new = delete_table(path.read_text(encoding="utf-8"), section, name)
            for model in users:
                new = delete_table(new, "models", model)
            Config.from_dict(tomllib.loads(new), path)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        write_file(path, new)
        reload(path)
        restart.note(f"{section}.{name} deleted")
        return {**view(), "note": saved_note(files)}

    def delete_source(cfg: Config, name: str, with_models: bool) -> dict[str, Any]:
        """Delete a model source and (with_models) its models. A built-in source is listed
        under `[removed] providers`, which also takes out its built-in models; a preset
        brings it back."""
        if name not in ai.providers(cfg):
            raise HTTPException(404, f"no provider {name!r}")
        users = [m.name for m in cfg.models.values() if m.provider == name]
        builtin = name in ai.BUILTIN_PROVIDERS
        hidden = [
            m.name
            for m in ai.BUILTIN_MODELS.values()
            if builtin and m.provider == name and m.name not in cfg.models
        ]
        if users and not with_models:
            raise HTTPException(409, f"provider {name!r} is used by models: {', '.join(users)}")
        gone = users + hidden
        defaults = [
            f"{a.model} (harness {a.name})" for a in cfg.harnesses.values() if a.model in gone
        ]
        if defaults:
            raise HTTPException(
                409, f"{name!r} has harness defaults: {', '.join(defaults)}; change those first"
            )
        path = config_path(files.config)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            new = delete_table(text, "providers", name)
            for model in users:
                new = delete_table(new, "models", model)
            if builtin:
                new = set_removed(new, [*cfg.removed_providers, name])
            Config.from_dict(tomllib.loads(new), path)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        write_file(path, new)
        reload(path)
        restart.note(f"providers.{name} deleted")
        return {**view(), "removed_models": gone, "note": saved_note(files)}

    def managed_check() -> None:
        """Keys always work here. In the Mac app (AGENTLINE_KEYS_MANAGED=file) the app's own
        Keys screen writes the same secrets file this API does, which the engine loads."""

    @router.put("/secrets/{name}")
    def put_secret(name: str, body: SecretBody) -> dict[str, Any]:
        managed_check()
        try:
            set_secret(name, body.value)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return {"keys": secret_status(key_names(config())), "note": saved_note(files)}

    @router.delete("/secrets/{name}")
    def remove_secret(name: str) -> dict[str, Any]:
        managed_check()
        try:
            found = delete_secret(name)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        if not found:
            raise HTTPException(404, f"{name} isn't in the secrets file")
        return {"keys": secret_status(key_names(config()))}

    @router.post("/providers/{name}/test")
    async def test_provider(name: str) -> dict[str, Any]:
        provider = ai.providers(config()).get(name)
        if provider is None:
            raise HTTPException(404, f"unknown provider {name!r}")
        try:
            found = await asyncio.to_thread(list_models, provider)
        except (ProviderProblem, ValidationError, OSError, ValueError) as e:
            return provider_failure(provider, e)
        if provider.type == "anthropic" and provider.auth == "subscription":
            # a subscription only works through the Claude Code CLI (and its login)
            cli = claude_bin(config())
            if cli["path"] is None:
                return {
                    "ok": False,
                    "error": f"the Claude Code CLI ({cli['bin']!r}) isn't installed or not on PATH",
                    "problem": "no_cli",
                    "bin": cli["bin"],
                    "models": found,
                }
            login = await asyncio.to_thread(claude_login, config(), True)
            if not login["logged_in"]:
                return {
                    "ok": False,
                    "error": "Claude Code is signed out"
                    + (f" ({login['error']})" if login.get("error") else ""),
                    "problem": "signed_out",
                    "models": found,
                }
            return {
                "ok": True,
                "count": len(found),
                "models": found,
                "cli": cli["path"],
                "note": "uses your Claude login",
            }
        return {"ok": True, "count": len(found), "models": found[:1000]}

    def subscription_cli(name: str) -> str | None:
        provider = ai.providers(config()).get(name)
        if provider is None:
            raise HTTPException(404, f"unknown provider {name!r}")
        if provider.auth != "subscription":
            raise HTTPException(422, f"{name!r} uses an API key, not a Claude login")
        return claude_bin(config())["path"]

    @router.get("/providers/{name}/login")
    async def login_status(name: str) -> dict[str, Any]:
        """The Claude Code login a subscription source uses (`claude auth status`)."""
        subscription_cli(name)
        return await asyncio.to_thread(claude_login, config(), True)

    @router.post("/providers/{name}/logout")
    async def logout(name: str) -> dict[str, Any]:
        """Sign Claude Code out. Every claude-code run on this machine loses the login until
        `claude auth login` is run again."""
        cli = subscription_cli(name)
        if cli is None:
            raise HTTPException(409, "the Claude Code CLI isn't installed")
        code, out, err = await _run(cli, "auth", "logout")
        forget_login()
        if code != 0:
            raise HTTPException(502, f"claude auth logout failed: {(err or out).strip()[:500]}")
        return await login_status(name)

    @router.put("/providers/{name}/models")
    def choose_models(name: str, body: ModelChoice) -> dict[str, Any]:
        """Make the source's configured models exactly `body.models` (by model id). New ids get
        a derived name; unchecked ones are removed unless built in or a harness's default.
        With `add_only`, only the missing ids are added."""
        cfg = config()
        if name not in ai.providers(cfg):
            raise HTTPException(404, f"unknown provider {name!r}")
        wanted = list(dict.fromkeys(m.strip() for m in body.models if m.strip()))
        every = ai.models(cfg)
        have = {m.model: m.name for m in every.values() if m.provider == name}
        defaults = {a.model for a in cfg.harnesses.values() if a.model}
        path = config_path(files.config)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        taken = set(every)
        added, removed, kept = [], [], []
        for model_id in wanted:
            if model_id in have:
                continue
            new_name = model_name_for(model_id, taken)
            taken.add(new_name)
            text = set_table(text, "models", new_name, {"provider": name, "model": model_id})
            added.append(new_name)
        for model_id, mname in have.items() if not body.add_only else ():
            if model_id in wanted or mname not in cfg.models:
                continue  # still wanted, or built in (nothing to delete)
            if mname in defaults:
                kept.append(mname)
                continue
            text = delete_table(text, "models", mname)
            removed.append(mname)
        if added or removed:
            try:
                Config.from_dict(tomllib.loads(text), path)
            except ValidationError as e:
                raise HTTPException(422, str(e)) from None
            write_file(path, text)
            reload(path)
            restart.note(f"models of {name} changed")
        kept_note = f"Kept {', '.join(kept)}: a harness's default model." if kept else None
        return {
            **view(),
            "added": added,
            "removed": removed,
            "kept": kept,
            "note": saved_note(files, kept_note) if added or removed else kept_note,
        }

    @router.post("/mcp/{name}/test")
    async def test_mcp(name: str) -> dict[str, Any]:
        server = config().mcp.get(name)
        if server is None:
            raise HTTPException(404, f"no MCP server {name!r}")
        return await asyncio.to_thread(test_server, server)

    @router.post("/mcp/test")
    async def test_mcp_draft(body: McpDraft) -> dict[str, Any]:
        try:
            server = parse_mcp(body.name or "draft", _clean(body.body))
            for k in body.secrets:
                from agentline.secrets import check_name

                check_name(k)
        except ValidationError as e:
            return {"ok": False, "error": str(e)}
        extra = {k: v for k, v in body.secrets.items() if v}
        return await asyncio.to_thread(test_server, server, 20, extra)

    def detected() -> list[mcp_detect.Found]:
        return mcp_detect.detect(
            project_dirs=mcp_detect.project_dirs(files.root),
            current_keys={**os.environ, **read_secrets()},
        )

    @router.get("/mcp/detect")
    async def detect_mcp() -> dict[str, Any]:
        found = await asyncio.to_thread(detected)
        return {"found": mcp_detect.public(found, config().mcp)}

    @router.post("/mcp/import")
    def import_mcp(body: McpImport) -> dict[str, Any]:
        cfg = config()
        try:
            picked = mcp_detect.import_found(detected(), body.items, cfg.mcp)
            path = config_path(files.config)
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            for name, table, _ in picked:
                text = set_table(text, "mcp", name, table)
            Config.from_dict(tomllib.loads(text), path)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        keys: list[str] = []
        for _, _, secrets in picked:  # values go from the source file to the secrets file only
            for k, v in secrets.items():
                if read_secrets().get(k) != v:
                    set_secret(k, v)
                keys.append(k)
        write_file(path, text)
        reload(path)
        restart.note("mcp servers imported")
        return {
            **view(),
            "imported": [n for n, _, _ in picked],
            "keys_set": sorted(set(keys)),
            "note": saved_note(files),
        }

    @router.post("/harnesses/{name}/detect")
    async def detect_harness(name: str) -> dict[str, Any]:
        profile = ai.harnesses(config()).get(name)
        if profile is None:
            raise HTTPException(404, f"unknown harness {name!r}")
        kind = ai.KINDS.get(ai.kind_name(profile.kind))
        if kind is None or kind.default_bin is None:
            return {"found": True, "version": None, "note": "no CLI needed (direct model call)"}
        binary = profile.bin or kind.default_bin
        path = shutil.which(binary)
        if path is None:
            return {
                "found": False,
                "bin": binary,
                "error": f"{binary!r} is not on PATH",
                "problem": "no_cli",
            }
        try:
            proc = await asyncio.create_subprocess_exec(
                path, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, err = await asyncio.wait_for(proc.communicate(), 15)
        except (OSError, TimeoutError) as e:
            return {"found": True, "bin": path, "error": str(e)}
        text = (out or err).decode(errors="replace").strip().splitlines()
        return {"found": True, "bin": path, "version": text[0] if text else None}

    return router
