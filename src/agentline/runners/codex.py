"""Runner `codex`: OpenAI Codex CLI, non-interactive (`codex exec --json ... -- <prompt>`).

`with:` params:
- `bin`: the CLI (else `$AGENTLINE_CODEX_BIN`, else `codex`).
- `model`: passed as `--model`; also the key for `[pricing.<model>]`.
- `sandbox`: `read-only` | `workspace-write` | `danger-full-access`; default derived from
  `allowed_tools` (below).
- `env`: host env var names to pass (default `[OPENAI_API_KEY]`). Nothing else from the host
  env reaches the CLI except PATH/HOME/locale basics when it runs locally.
- `input_usd_per_mtok` / `output_usd_per_mtok`: prices (else `[pricing.<model>]`).
- `extra_args`: appended to the default flags (before the prompt).
- `args`: replaces the default flags entirely, for CLI versions with different flags.
  Placeholders: `{prompt}`, `{model}`, `{sandbox}`; without `{prompt}` the prompt goes last.
  E.g. `args: [exec, --json, --full-auto, --model, "{model}", "{prompt}"]`.

`allowed_tools` -> Codex has no per-tool allowlist, only an OS sandbox, so the mapping is coarse:
- no `edit`/`bash` -> `--sandbox read-only`: the agent can still run shell commands, but the
  sandbox blocks file writes and network. `read` can't be taken away.
- `edit` or `bash` -> `--sandbox workspace-write`: writes are allowed only in the working
  directory (and temp dirs), network is off. `edit` without `bash` can't be enforced: Codex
  edits files by running commands, so both mean the same sandbox.
- `web` -> `-c tools.web_search=true` (Codex's own web search tool). It doesn't open general
  network access for commands.
`codex exec` never asks for approval, so there is no approval flag. In a container where the
Linux sandbox (Landlock/seccomp) isn't available, set `sandbox: danger-full-access` and rely
on the container's isolation. `max_turns` has no Codex equivalent and isn't enforced; the
node's `timeout` is.

Cost: Codex reports tokens (`turn.completed.usage`), not money. Cost = tokens x the price
(cached input billed as input); with no price known it is 0 and `meta.warnings` says so.
The budget is enforced after the run, as for claude-code.
"""

from __future__ import annotations

import json
from typing import Any

from agentline.errors import BudgetExceeded
from agentline.mcp import codex_args as codex_mcp_args
from agentline.registry import RunRequest, RunResult
from agentline.runners.agent_cli import (
    BinaryMissingInWorkspace,
    cli_bin,
    env_names,
    exec_cli,
    render_args,
)
from agentline.runners.anthropic import cost_of, price_for
from agentline.runners.common import RunnerError, detect_missing_tools, parse_json_text

DEFAULT_ENV = ("OPENAI_API_KEY",)
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


def sandbox_for(request: RunRequest) -> str:
    chosen = request.params.get("sandbox")
    if chosen:
        return str(chosen)
    tools = set(request.limits.allowed_tools)
    return "workspace-write" if tools & {"edit", "bash"} else "read-only"


def build_argv(request: RunRequest) -> list[str]:
    p = request.params
    binary = cli_bin(p, "AGENTLINE_CODEX_BIN", "codex")
    model = str(p["model"]) if p.get("model") else None
    sandbox = sandbox_for(request)
    if p.get("args") is not None:
        args, placed = render_args(
            p["args"], {"prompt": request.prompt, "model": model, "sandbox": sandbox}
        )
        return [binary, *args] if placed else [binary, *args, request.prompt]
    argv = [binary, "exec", "--json", "--sandbox", sandbox]
    if model:
        argv += ["--model", model]
    if "web" in request.limits.allowed_tools:
        argv += ["-c", "tools.web_search=true"]
    if request.workspace is None:
        argv.append("--skip-git-repo-check")  # the host cwd may not be a git repo
    servers = p.get("mcp_servers") or {}
    if servers:  # the node's MCP servers (agentline.mcp)
        argv += codex_mcp_args(servers)
    argv += [str(x) for x in p.get("extra_args") or ()]
    return [*argv, "--", request.prompt]


def parse_events(stdout: str) -> dict[str, Any]:
    """Fold Codex's JSONL events. Handles the `thread.started / item.completed /
    turn.completed` format and the older `{"msg": {"type": ...}}` one."""
    messages: list[str] = []
    failed_cmds: list[str] = []
    errors: list[str] = []
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    events = 0
    thread_id = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        events += 1
        if isinstance(ev.get("msg"), dict):  # older format
            ev = {**ev["msg"], "type": "legacy." + str(ev["msg"].get("type"))}
        t = str(ev.get("type") or "")
        if t == "thread.started":
            thread_id = ev.get("thread_id")
        elif t == "item.completed" and isinstance(ev.get("item"), dict):
            item = ev["item"]
            kind = item.get("type") or item.get("item_type")
            if kind in ("agent_message", "assistant_message"):
                messages.append(str(item.get("text") or ""))
            elif kind == "command_execution" and item.get("exit_code") in (126, 127):
                failed_cmds.append(str(item.get("aggregated_output") or ""))
            elif kind == "error":
                errors.append(str(item.get("message") or "error"))
        elif t == "turn.completed" and isinstance(ev.get("usage"), dict):
            for k in usage:
                usage[k] += int(ev["usage"].get(k) or 0)
        elif t in ("turn.failed", "error"):
            err = ev.get("error")
            msg = err.get("message") if isinstance(err, dict) else ev.get("message")
            errors.append(str(msg or t))
        elif t == "legacy.agent_message":
            messages.append(str(ev.get("message") or ""))
        elif t == "legacy.token_count":
            usage["input_tokens"] += int(ev.get("input_tokens") or 0)
            usage["cached_input_tokens"] += int(ev.get("cached_input_tokens") or 0)
            usage["output_tokens"] += int(ev.get("output_tokens") or 0)
        elif t == "legacy.error":
            errors.append(str(ev.get("message") or "error"))
    return {
        "events": events,
        "result": messages[-1] if messages else "",
        "failed_commands": failed_cmds,
        "errors": errors,
        "usage": usage,
        "thread_id": thread_id,
    }


class CodexRunner:
    name = "codex"

    def __init__(self, pricing: dict[str, Any] | None = None) -> None:
        self.pricing = dict(pricing or {})

    async def run(self, request: RunRequest) -> RunResult:
        budget = request.limits.budget_usd
        if budget is not None and budget <= 0:
            raise BudgetExceeded(0.0, 0.0, "call")
        p = request.params
        argv = build_argv(request)
        try:
            code, out, err = await exec_cli(request, argv, env_names(p, DEFAULT_ENV), "codex")
        except BinaryMissingInWorkspace as e:
            return RunResult(None, e.output, missing_tools=[e.tool])
        transcript = out + (f"\n--- stderr ---\n{err}" if err.strip() else "")
        data = parse_events(out)
        if data["events"] == 0:
            tail = (err or out).strip()[-500:]
            raise RunnerError(f"codex exited {code} without JSON events: {tail}")

        model = str(p["model"]) if p.get("model") else None
        meta: dict[str, Any] = {
            "model": model,
            "thread_id": data["thread_id"],
            "usage": data["usage"],
            "exit_code": code,
        }
        price = price_for(model or "", p, self.pricing)
        if price is None:
            cost = 0.0
            meta["warnings"] = [
                f"no price known for model {model!r}; cost recorded as 0"
                " (set [pricing.<model>] or input_usd_per_mtok/output_usd_per_mtok)"
            ]
        else:
            cost = cost_of(data["usage"], price)  # input_tokens already includes cached
        result_text = data["result"]
        missing = detect_missing_tools(result_text, *data["failed_commands"])
        if missing:
            return RunResult(None, transcript, cost, missing_tools=missing, meta=meta)
        if data["errors"] or code != 0:
            why = "; ".join(data["errors"]) or f"exit {code}"
            raise RunnerError(
                f"codex reported an error ({why[-300:]})",
                cost_usd=cost,
                transcript=transcript,
                meta=meta,
            )
        if budget is not None and cost > budget:
            meta["budget_exceeded"] = {"cost_usd": cost, "budget_usd": budget}
        ok, value = parse_json_text(result_text)
        return RunResult(
            output=value if ok else result_text, transcript=transcript, cost_usd=cost, meta=meta
        )
