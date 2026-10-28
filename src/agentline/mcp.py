"""MCP servers: configured once in agentline.toml, shared by nodes that allow them.

    [mcp.github]
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-github"]
    env = { GITHUB_PERSONAL_ACCESS_TOKEN = "${GITHUB_TOKEN}" }

    [mcp.linear]
    url = "https://mcp.linear.app/mcp"
    transport = "http"            # http (streamable) | sse
    headers = { Authorization = "Bearer ${LINEAR_API_KEY}" }

`enabled = false` keeps a server configured but off: nodes that list it run without it.
`${NAME}` in `env` / `headers` / `args` is filled from the environment (the server's form) when a
node runs, so secrets stay out of the file. An agent node gets only the servers it lists
(`mcp: [github]`), for claude-code (a private `--mcp-config` file with `--strict-mcp-config`,
and `mcp__<name>` added to its allowed tools) and codex (`-c mcp_servers.<name>.*`).
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentline.errors import ValidationError

TRANSPORTS = ("stdio", "http", "sse")
MCP_KINDS = ("claude-code", "codex")  # harness kinds that can load MCP servers
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRETISH = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|xox[bp]-|Bearer [A-Za-z0-9._-]{12,})"
)


@dataclass(frozen=True)
class McpServer:
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    description: str = ""
    enabled: bool = True
    probe_safe: bool = False  # run reviews may replay nodes with it (agentline.probe)

    def key_names(self) -> list[str]:
        """The `${NAME}` variables it needs."""
        text = " ".join([*self.args, *self.env.values(), *self.headers.values()])
        return sorted(set(_VAR.findall(text)))


def _str_map(t: dict[str, Any], key: str, where: str) -> dict[str, str]:
    v = t.get(key) or {}
    if not isinstance(v, dict) or not all(isinstance(x, str) for x in v.values()):
        raise ValidationError(f"{where}.{key} must be a table of strings")
    for k, x in v.items():
        if _SECRETISH.search(x) and not _VAR.search(x):
            raise ValidationError(
                f"{where}.{key}.{k} looks like a secret: type it in the server's secret field"
                " and write ${NAME}"
            )
    return {str(k): x for k, x in v.items()}


def parse_mcp(name: str, t: dict[str, Any]) -> McpServer:
    where = f"[mcp.{name}]"
    url = t.get("url")
    command = t.get("command")
    transport = str(t.get("transport") or ("http" if url else "stdio"))
    if transport not in TRANSPORTS:
        raise ValidationError(f"{where}.transport must be one of {', '.join(TRANSPORTS)}")
    if transport == "stdio" and not command:
        raise ValidationError(f"{where} needs `command` (or a `url` for an http/sse server)")
    if transport != "stdio" and not url:
        raise ValidationError(f"{where} needs `url` for transport {transport}")
    enabled = t.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValidationError(f"{where}.enabled must be true or false")
    probe_safe = t.get("probe_safe", False)
    if not isinstance(probe_safe, bool):
        raise ValidationError(f"{where}.probe_safe must be true or false")
    args = t.get("args") or []
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        raise ValidationError(f"{where}.args must be a list of strings")
    return McpServer(
        name=name,
        transport=transport,
        command=str(command) if command else None,
        args=tuple(args),
        env=_str_map(t, "env", where),
        url=str(url) if url else None,
        headers=_str_map(t, "headers", where),
        description=str(t.get("description", "")),
        enabled=enabled,
        probe_safe=probe_safe,
    )


def fill(text: str, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ

    def sub(m: re.Match[str]) -> str:
        v = env.get(m.group(1))
        if v is None:
            raise ValidationError(
                f"MCP server needs {m.group(1)}, which isn't set (Settings > MCP, Edit)"
            )
        return v

    return _VAR.sub(sub, text)


def resolved(s: McpServer, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """The server as Claude Code's mcpServers entry, with ${NAME} filled in."""
    if s.transport == "stdio":
        out: dict[str, Any] = {
            "type": "stdio",
            "command": s.command,
            "args": [fill(a, environ) for a in s.args],
        }
        if s.env:
            out["env"] = {k: fill(v, environ) for k, v in s.env.items()}
        return out
    out = {"type": s.transport, "url": s.url}
    if s.headers:
        out["headers"] = {k: fill(v, environ) for k, v in s.headers.items()}
    return out


def public(s: McpServer) -> dict[str, Any]:
    d = {
        "name": s.name,
        "transport": s.transport,
        "command": s.command,
        "args": list(s.args),
        "env": dict(s.env),
        "url": s.url,
        "headers": dict(s.headers),
        "description": s.description,
        "keys": s.key_names(),
        "enabled": s.enabled,
        "probe_safe": s.probe_safe or None,
    }
    keep = ("name", "transport", "enabled")
    return {k: v for k, v in d.items() if v not in (None, "", [], {}) or k in keep}


def used_by(files: Any) -> dict[str, list[dict[str, str]]]:
    """Server name -> the pipeline nodes that allow it ({pipeline, node}). `files` is the
    editor's PipelineFiles; pipelines that don't parse are skipped."""
    from agentline.pipeline.spec import parse_spec

    out: dict[str, list[dict[str, str]]] = {}
    for path in files.files():
        try:
            spec = parse_spec(path.read_text(), path=path, base_dir=path.parent)
        except Exception:  # a broken pipeline: the editor shows why
            continue
        for n in spec.nodes.values():
            for m in n.mcp:
                out.setdefault(m, []).append({"pipeline": files.name_of(path), "node": n.id})
    return out


# -- test: initialize + tools/list ------------------------------------------------------------
_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "agentline", "version": "1"},
    },
}
_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def _tools_from(msg: dict[str, Any]) -> list[dict[str, str]]:
    tools = (msg.get("result") or {}).get("tools") or []
    return [
        {"name": str(t.get("name")), "description": str(t.get("description") or "")[:200]}
        for t in tools
    ]


def test_server(
    s: McpServer, timeout: float = 20, extra: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Connect, initialize and list the tools. {ok, tools | error}. `extra` are key values
    typed into the new-server popup and not saved yet: used for this test only."""
    environ = {**os.environ, **(extra or {})}
    try:
        if s.transport == "stdio":
            return _test_stdio(s, timeout, environ)
        return _test_http(s, timeout, environ)
    except ValidationError as e:
        return {"ok": False, "error": str(e)}
    except FileNotFoundError:
        return {"ok": False, "error": f"{s.command!r} isn't installed or not on PATH"}
    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _test_stdio(s: McpServer, timeout: float, environ: dict[str, str]) -> dict[str, Any]:
    env = {**os.environ, **{k: fill(v, environ) for k, v in s.env.items()}}
    argv = [str(s.command), *[fill(a, environ) for a in s.args]]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:  # a thread: readline() buffers, so select() can't be trusted
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(msg: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def answer(want: int) -> dict[str, Any] | None:
        while (left := deadline - time.monotonic()) > 0:
            try:
                line = lines.get(timeout=left)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # a server logging to stdout
            if isinstance(msg, dict) and msg.get("id") == want:
                return msg
        return None

    try:
        send(_INIT)
        init = answer(1)
        if init is not None and "error" not in init:
            send(_INITIALIZED)
            send(_TOOLS)
            tools = answer(2)
            if tools is not None and "error" not in tools:
                server = (init.get("result") or {}).get("serverInfo")
                return {"ok": True, "server": server, "tools": _tools_from(tools)}
            if tools is not None:
                return {"ok": False, "error": f"tools/list failed: {tools['error']}"}
        elif init is not None:
            return {"ok": False, "error": f"initialize failed: {init['error']}"}
        err = ""
        if proc.poll() is not None and proc.stderr is not None:
            err = proc.stderr.read()[-400:]
        detail = f": {err.strip()}" if err.strip() else ""
        return {"ok": False, "error": "no answer from the server" + detail}
    except BrokenPipeError:
        err = proc.stderr.read()[-400:] if proc.stderr is not None else ""
        return {"ok": False, "error": f"the server exited: {err.strip()}"}
    finally:
        proc.kill()
        proc.wait(5)


def _post(
    url: str, body: dict[str, Any], headers: dict[str, str], session: str | None, timeout: float
) -> tuple[dict[str, Any] | None, str | None]:
    h = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        **headers,
    }
    if session:
        h["mcp-session-id"] = session
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sid = r.headers.get("mcp-session-id") or session
        text = r.read().decode(errors="replace")
        ctype = r.headers.get("content-type", "")
    if not text.strip():
        return None, sid
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip()), sid
                except ValueError:
                    continue
        return None, sid
    return json.loads(text), sid


def _test_http(s: McpServer, timeout: float, environ: dict[str, str]) -> dict[str, Any]:
    if s.transport == "sse":
        return {"ok": True, "tools": [], "note": "SSE servers are checked when an agent connects"}
    headers = {k: fill(v, environ) for k, v in s.headers.items()}
    url = str(s.url)
    try:
        init, sid = _post(url, _INIT, headers, None, timeout)
        _post(url, _INITIALIZED, headers, sid, timeout)
        tools, _ = _post(url, _TOOLS, headers, sid, timeout)
    except urllib.error.HTTPError as e:
        hint = ": check the key in headers" if e.code in (401, 403) else ""
        return {"ok": False, "error": f"HTTP {e.code} from {url}{hint}"}
    server = ((init or {}).get("result") or {}).get("serverInfo")
    return {"ok": True, "server": server, "tools": _tools_from(tools or {})}


# -- runner configs ---------------------------------------------------------------------------
def claude_config(servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"mcpServers": servers}


def codex_args(servers: dict[str, dict[str, Any]]) -> list[str]:
    """`-c mcp_servers.<name>.<key>=<toml>` overrides for `codex exec`."""
    out: list[str] = []
    for name, s in servers.items():
        base = f"mcp_servers.{name}"
        if s.get("type") == "stdio":
            out += ["-c", f"{base}.command={json.dumps(s['command'])}"]
            out += ["-c", f"{base}.args={json.dumps(s.get('args') or [])}"]
            env = s.get("env") or {}
            if env:
                pairs = ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in env.items())
                out += ["-c", f"{base}.env={{ {pairs} }}"]
        else:
            out += ["-c", f"{base}.url={json.dumps(s['url'])}"]
    return out
