"""Runner `opencode`: OpenCode, non-interactive (`opencode run --format json -- <prompt>`).

`with:` params:
- `bin`: the CLI (else `$AGENTLINE_OPENCODE_BIN`, else `opencode`).
- `model`: passed as `--model`, in OpenCode's `provider/model` form (agents.py builds it:
  `anthropic/<id>` for an Anthropic source, `agentline/<id>` for an OpenAI-compatible one,
  which `env_set` declares through `OPENCODE_CONFIG_CONTENT`). Also the key for
  `[pricing.<model>]`.
- `agent`: an OpenCode agent name (`--agent`).
- `env`: host env var names to pass (default `[ANTHROPIC_API_KEY, OPENAI_API_KEY,
  OPENROUTER_API_KEY]`). Nothing else from the host env reaches the CLI except PATH/HOME/locale
  basics when it runs locally.
- `input_usd_per_mtok` / `output_usd_per_mtok`: prices, used only when OpenCode reports none.
- `extra_args`: appended to the default flags (before the prompt).
- `args`: replaces the default flags entirely, for CLI versions with different flags.
  Placeholders: `{prompt}`, `{model}`; without `{prompt}` the prompt goes last.

`allowed_tools` -> OpenCode permissions (`OPENCODE_PERMISSION`, merged over the user's config):
- no `edit` -> `edit: deny`; no `bash` -> `bash: deny`; no `web` -> `webfetch: deny`.
- `edit` or `bash` -> `--auto` too, so permissions left at "ask" are approved instead of
  waiting for a person who isn't there. `read` can't be taken away.
`max_turns` has no OpenCode flag and isn't enforced; the node's `timeout` is.

Cost: OpenCode reports a cost per step (`step_finish` events); their sum is the cost. With none
reported, tokens x the price; with no price either, 0 and `meta.warnings` says so.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from agentline.errors import BudgetExceeded
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

DEFAULT_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")


def permissions(allowed: tuple[str, ...] | list[str]) -> dict[str, str]:
    tools = set(allowed)
    return {
        name: "deny"
        for tool, name in (("edit", "edit"), ("bash", "bash"), ("web", "webfetch"))
        if tool not in tools
    }


def build_argv(request: RunRequest) -> list[str]:
    p = request.params
    binary = cli_bin(p, "AGENTLINE_OPENCODE_BIN", "opencode")
    model = str(p["model"]) if p.get("model") else None
    if p.get("args") is not None:
        args, placed = render_args(p["args"], {"prompt": request.prompt, "model": model})
        return [binary, *args] if placed else [binary, *args, request.prompt]
    argv = [binary, "run", "--format", "json"]
    if model:
        argv += ["--model", model]
    if p.get("agent"):
        argv += ["--agent", str(p["agent"])]
    if set(request.limits.allowed_tools) & {"edit", "bash"}:
        argv.append("--auto")
    argv += [str(x) for x in p.get("extra_args") or ()]
    return [*argv, "--", request.prompt]


def _error_text(err: Any) -> str:
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        return str(err.get("message") or err.get("name") or "error")
    return str(err or "error")


def parse_events(stdout: str) -> dict[str, Any]:
    """Fold OpenCode's JSON events (`text`, `tool_use`, `step_finish`, `error`, each with a
    `part`). Unknown events are skipped, so newer CLI versions still parse."""
    texts: list[str] = []
    failed: list[str] = []
    errors: list[str] = []
    usage = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    cost: float | None = None
    events = 0
    session = None
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
        session = ev.get("sessionID") or session
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        t = str(ev.get("type") or "")
        if t == "text" and part.get("text"):
            texts.append(str(part["text"]))
        elif t == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            if state.get("status") == "error" or "not found" in str(state.get("output") or ""):
                failed.append(str(state.get("output") or state.get("error") or ""))
        elif t == "step_finish":
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            usage["input_tokens"] += int(tokens.get("input") or 0)
            usage["output_tokens"] += int(tokens.get("output") or 0) + int(
                tokens.get("reasoning") or 0
            )
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            usage["cache_read_input_tokens"] += int(cache.get("read") or 0)
            if isinstance(part.get("cost"), int | float):
                cost = (cost or 0.0) + float(part["cost"])
        elif t == "error":
            errors.append(_error_text(ev.get("error") or part.get("error")))
    return {
        "events": events,
        "result": texts[-1] if texts else "",
        "failed_commands": failed,
        "errors": errors,
        "usage": usage,
        "cost": cost,
        "session_id": session,
    }


class OpenCodeRunner:
    name = "opencode"

    def __init__(self, pricing: dict[str, Any] | None = None) -> None:
        self.pricing = dict(pricing or {})

    async def run(self, request: RunRequest) -> RunResult:
        budget = request.limits.budget_usd
        if budget is not None and budget <= 0:
            raise BudgetExceeded(0.0, 0.0, "call")
        p = request.params
        argv = build_argv(request)
        deny = permissions(request.limits.allowed_tools)
        if deny:  # through the env, so the user's own opencode.json is left alone
            env_set = {**dict(p.get("env_set") or {}), "OPENCODE_PERMISSION": json.dumps(deny)}
            request = replace(request, params={**p, "env_set": env_set})
        try:
            code, out, err = await exec_cli(request, argv, env_names(p, DEFAULT_ENV), "opencode")
        except BinaryMissingInWorkspace as e:
            return RunResult(None, e.output, missing_tools=[e.tool])
        transcript = out + (f"\n--- stderr ---\n{err}" if err.strip() else "")
        data = parse_events(out)
        if data["events"] == 0:
            tail = (err or out).strip()[-500:]
            raise RunnerError(f"opencode exited {code} without JSON events: {tail}")

        model = str(p["model"]) if p.get("model") else None
        meta: dict[str, Any] = {
            "model": model,
            "session_id": data["session_id"],
            "usage": data["usage"],
            "exit_code": code,
        }
        cost = data["cost"]
        if cost is None:
            price = price_for(model or "", p, self.pricing)
            if price is None:
                cost = 0.0
                meta["warnings"] = [
                    f"opencode reported no cost and no price is known for {model!r};"
                    " recorded as 0 (set [pricing.<model>] or input/output_usd_per_mtok)"
                ]
            else:
                cost = cost_of(data["usage"], price)
        result_text = data["result"]
        missing = detect_missing_tools(result_text, *data["failed_commands"])
        if missing:
            return RunResult(None, transcript, cost, missing_tools=missing, meta=meta)
        if data["errors"] or code != 0:
            why = "; ".join(data["errors"]) or f"exit {code}"
            raise RunnerError(
                f"opencode reported an error ({why[-300:]})",
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
