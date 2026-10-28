"""Find MCP servers already set up on this machine and import them into agentline.toml.

Looked at (each only if it exists):
  Claude Code      ~/.claude.json            `mcpServers` (user) and `projects.<dir>.mcpServers`
  Claude Desktop   claude_desktop_config.json (macOS Application Support, ~/.config, %APPDATA%)
  Codex            ~/.codex/config.toml      `[mcp_servers.<name>]` ($CODEX_HOME wins)
  Cursor           ~/.cursor/mcp.json
  Project          .mcp.json in the pipelines folder, its parent and the working directory

Secret values never reach agentline.toml or the UI. On detection every value that looks like a
secret (env vars named like a key/token, Authorization and other headers, secret-looking args
and URL query values) becomes a `${KEY}` placeholder; the value itself only travels from the
source file to the secrets file when the user imports the server. A key that is already set to
another value gets a server-specific name instead of being overwritten.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agentline.errors import ValidationError
from agentline.mcp import _SECRETISH, McpServer, parse_mcp

_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
_SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|SESSION|PRIVATE"
    r"|(^|_)PAT($|_)|(^|_)AUTH)",
    re.I,
)
_SECRET_FLAG = re.compile(r"^--?[\w-]*(key|token|secret|password|auth)[\w-]*$", re.I)
_OPAQUE = re.compile(r"^(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9_\-.=+/]{24,}$")


@dataclass
class Found:
    """One server found in another tool's config, already in agentline's shape."""

    id: str
    source: str
    path: str
    name: str
    table: dict[str, Any]
    secrets: dict[str, str] = field(default_factory=dict)  # KEY -> value (never sent out)
    where: dict[str, str] = field(default_factory=dict)  # KEY -> "env GITHUB_TOKEN"
    problem: str | None = None


# -- reading the sources ----------------------------------------------------------------------
def _json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _desktop_paths(home: Path) -> list[Path]:
    out = [
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        home / ".config" / "Claude" / "claude_desktop_config.json",
    ]
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        out.append(Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json")
    return out


def _sources(home: Path, project_dirs: list[Path]) -> list[tuple[str, str, Path, Any]]:
    """(id prefix, label, file, {name: raw entry}) for every config that exists."""
    out: list[tuple[str, str, Path, Any]] = []
    cc = home / ".claude.json"
    if (data := _json(cc)) is not None:
        out.append(("claude-code", "Claude Code", cc, data.get("mcpServers") or {}))
        for d, proj in (data.get("projects") or {}).items():
            if isinstance(proj, dict) and proj.get("mcpServers"):
                out.append(
                    (f"claude-code:{d}", f"Claude Code ({Path(d).name})", cc, proj["mcpServers"])
                )
    for p in _desktop_paths(home):
        if (data := _json(p)) is not None:
            out.append(("claude-desktop", "Claude Desktop", p, data.get("mcpServers") or {}))
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    codex = codex_home / "config.toml"
    if codex.is_file():
        try:
            servers = tomllib.loads(codex.read_text()).get("mcp_servers") or {}
            out.append(("codex", "Codex", codex, {k: _from_codex(v) for k, v in servers.items()}))
        except (OSError, tomllib.TOMLDecodeError):
            pass
    cursor = home / ".cursor" / "mcp.json"
    if (data := _json(cursor)) is not None:
        out.append(("cursor", "Cursor", cursor, data.get("mcpServers") or {}))
    seen: set[Path] = set()
    for d in project_dirs:
        p = (d / ".mcp.json").resolve()
        if p in seen or (data := _json(p)) is None:
            continue
        seen.add(p)
        out.append((f"project:{p.parent}", f"Project {p.parent.name}", p, data.get("mcpServers")))
    return out


def _from_codex(v: Any) -> Any:
    """A Codex [mcp_servers.x] table as a Claude-style entry."""
    if not isinstance(v, dict):
        return v
    entry: dict[str, Any] = {k: v[k] for k in ("command", "args", "env", "url") if k in v}
    headers = dict(v.get("http_headers") or {})
    for h, var in (v.get("env_http_headers") or {}).items():
        headers[h] = f"${{{var}}}"
    if v.get("bearer_token_env_var"):
        headers["Authorization"] = f"Bearer ${{{v['bearer_token_env_var']}}}"
    if headers:
        entry["headers"] = headers
    env = dict(entry.get("env") or {})
    for var in v.get("env_vars") or []:  # passed through from the environment
        env.setdefault(str(var), f"${{{var}}}")
    if env:
        entry["env"] = env
    return entry


# -- turning an entry into a table + secrets --------------------------------------------------
def _key_name(text: str) -> str:
    k = re.sub(r"[^A-Z0-9_]+", "_", text.upper()).strip("_") or "KEY"
    return "_" + k if k[0].isdigit() else k


class _Keys:
    """Picks key names: reuses a key already set to the same value, and never takes over a key
    that holds a different value (or one another server in this import already uses)."""

    def __init__(self, current: dict[str, str]) -> None:
        self.current = current
        self.taken: dict[str, str] = {}

    def name(self, wanted: str, value: str, server: str) -> str:
        cands = [wanted, f"{wanted}_{_key_name(server)}"] if server else [wanted]
        cands += [f"{cands[-1]}_{i}" for i in range(2, 50)]
        for k in cands:
            have = self.taken.get(k, self.current.get(k))
            if have is None or have == value:
                self.taken[k] = value
                return k
        raise ValidationError(f"no free key name for {wanted}")


def _secret_env(name: str, value: str) -> bool:
    return bool(_SECRET_NAME.search(name) or _SECRETISH.search(value) or _OPAQUE.match(value))


def convert(
    name: str, raw: Any, keys: _Keys
) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """(table for agentline.toml, {KEY: value}, {KEY: where it came from})."""
    if not isinstance(raw, dict):
        raise ValidationError("not a server entry")
    secrets: dict[str, str] = {}
    where: dict[str, str] = {}
    server = _key_name(name)

    def secret(wanted: str, value: str, origin: str) -> str:
        k = keys.name(wanted, value, server)
        secrets[k], where[k] = value, origin
        return k

    def refs(v: str) -> str:  # ${X:-default} -> ${X}
        return _REF.sub(lambda m: "${" + m.group(1) + "}", v)

    table: dict[str, Any] = {}
    url = raw.get("url") or raw.get("serverUrl")
    kind = str(raw.get("type") or raw.get("transport") or ("http" if url else "stdio"))
    kind = {"streamable-http": "http", "streamableHttp": "http", "local": "stdio"}.get(kind, kind)
    table["transport"] = kind
    if kind == "stdio":
        table["command"] = str(raw.get("command") or "")
        args: list[str] = []
        flag_wants_secret = False
        for i, a in enumerate(str(x) for x in raw.get("args") or []):
            a = refs(a)
            if _REF.fullmatch(a):
                args.append(a)
            elif flag_wants_secret and not a.startswith("-"):
                args.append("${" + secret(f"{server}_{_key_name(args[-1])}", a, f"arg {i}") + "}")
            elif "=" in a and _SECRET_FLAG.match(a.split("=", 1)[0]):
                flag, val = a.split("=", 1)
                k = secret(f"{server}_{_key_name(flag)}", val, f"arg {flag}")
                args.append(f"{flag}=${{{k}}}")
            elif m := _SECRETISH.search(a):
                k = secret(f"{server}_KEY", m.group(0), f"arg {i}")
                args.append(a.replace(m.group(0), "${" + k + "}"))
            else:
                args.append(a)
            flag_wants_secret = bool(_SECRET_FLAG.match(a)) and "=" not in a
        if args:
            table["args"] = args
        env = {}
        for k, v in (raw.get("env") or {}).items():
            v = refs(str(v))
            if _REF.search(v) or not v or not _secret_env(str(k), v):
                env[str(k)] = v
            else:
                env[str(k)] = "${" + secret(_key_name(str(k)), v, f"env {k}") + "}"
        if env:
            table["env"] = env
    else:
        parts = urlsplit(refs(str(url or "")))
        query = []
        for qk, qv in parse_qsl(parts.query, keep_blank_values=True):
            if qv and (_SECRET_NAME.search(qk) or _SECRETISH.search(qv)) and not _REF.search(qv):
                qv = "${" + secret(f"{server}_{_key_name(qk)}", qv, f"url ?{qk}") + "}"
            query.append((qk, qv))
        q = urlencode(query, safe="${}")
        table["url"] = urlunsplit(parts._replace(query=q))
        headers = {}
        for h, v in (raw.get("headers") or {}).items():
            v = refs(str(v))
            if _REF.search(v) or not v:
                headers[str(h)] = v
                continue
            scheme, _, rest = v.partition(" ")
            if h.lower() == "authorization" and rest and scheme.lower() in ("bearer", "basic"):
                headers[str(h)] = f"{scheme} ${{{secret(f'{server}_TOKEN', rest, f'header {h}')}}}"
            else:
                headers[str(h)] = "${" + secret(f"{server}_{_key_name(h)}", v, f"header {h}") + "}"
        if headers:
            table["headers"] = headers
    if raw.get("description"):
        table["description"] = str(raw["description"])
    return table, secrets, where


def safe_name(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "server"
    if not re.match(r"^[A-Za-z0-9_]", base):
        base = "s-" + base
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def detect(
    home: Path | None = None,
    project_dirs: list[Path] | None = None,
    current_keys: dict[str, str] | None = None,
) -> list[Found]:
    """Every MCP server found on this machine, converted (secrets split out)."""
    home = home or Path.home()
    keys = _Keys(dict(current_keys if current_keys is not None else os.environ))
    out: list[Found] = []
    for prefix, label, path, servers in _sources(home, project_dirs or []):
        if not isinstance(servers, dict):
            continue
        for name, raw in servers.items():
            f = Found(id=f"{prefix}:{name}", source=label, path=str(path), name=str(name), table={})
            try:
                f.table, f.secrets, f.where = convert(str(name), raw, keys)
                parse_mcp(str(name), f.table)
            except ValidationError as e:
                f.problem = str(e)
            out.append(f)
    return out


def _same(a: McpServer, table: dict[str, Any]) -> bool:
    try:
        b = parse_mcp(a.name, table)
    except ValidationError:
        return False
    return (a.transport, a.command, a.args, a.url) == (b.transport, b.command, b.args, b.url)


def public(found: list[Found], existing: dict[str, McpServer]) -> list[dict[str, Any]]:
    """What the UI and the Chat see: never a secret value. `status`: new | configured (the
    same server is already in agentline.toml) | problem; `suggested` is a free name."""
    taken = set(existing)
    out = []
    for f in found:
        match = next((s.name for s in existing.values() if _same(s, f.table)), None)
        status = "problem" if f.problem else "configured" if match else "new"
        suggested = match or safe_name(f.name, taken)
        if status == "new":
            taken.add(suggested)
        out.append(
            {
                "id": f.id,
                "source": f.source,
                "path": f.path,
                "name": f.name,
                "suggested": suggested,
                "status": status,
                "problem": f.problem,
                **{k: v for k, v in f.table.items()},
                "secrets": [{"key": k, "from": f.where[k]} for k in sorted(f.secrets)],
            }
        )
    return out


def import_found(
    found: list[Found], picks: list[dict[str, str]], existing: dict[str, McpServer]
) -> list[tuple[str, dict[str, Any], dict[str, str]]]:
    """[(name, table, secrets)] for the picked ids ({id, name?}); raises on a bad pick."""
    by_id = {f.id: f for f in found}
    taken = set(existing)
    out = []
    for pick in picks:
        f = by_id.get(str(pick.get("id")))
        if f is None:
            raise ValidationError(f"no detected server {pick.get('id')!r} (detect again)")
        if f.problem:
            raise ValidationError(f"{f.id} can't be imported: {f.problem}")
        name = str(pick.get("name") or "") or safe_name(f.name, taken)
        if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$", name):
            raise ValidationError(f"bad name {name!r}")
        if name in taken:
            raise ValidationError(f"an MCP server named {name!r} already exists")
        taken.add(name)
        parse_mcp(name, f.table)
        out.append((name, f.table, f.secrets))
    return out


def project_dirs(pipelines_root: Path | None) -> list[Path]:
    dirs = [Path.cwd()]
    if pipelines_root is not None:
        dirs = [pipelines_root, pipelines_root.parent, *dirs]
    return dirs
