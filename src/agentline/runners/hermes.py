"""Runner `hermes`: Nous Research's Hermes Agent, one query and exit
(`hermes chat --query=<prompt> --oneshot --format stream-json`).

`with:` params:
- `bin`: the CLI (else `$AGENTLINE_HERMES_BIN`, else `hermes`).
- `model`: passed as `--model`; also the key for `[pricing.<model>]`.
- `provider`: passed as `--provider` (agents.py sets it from the model's source: `anthropic`,
  `openrouter`, or `custom` with `base_url` for any other OpenAI-compatible server).
- `base_url`: `--base-url`, for `provider: custom`. The key never goes on the command line: it
  reaches Hermes as `OPENAI_API_KEY` / `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` (`env_map`).
- `toolsets`: Hermes toolsets (`-t`), replacing the ones derived from `allowed_tools` below.
- `env`: host env var names to pass (default `[ANTHROPIC_API_KEY, OPENAI_API_KEY,
  OPENROUTER_API_KEY]`). Nothing else from the host env reaches the CLI except PATH/HOME/locale
  basics when it runs locally.
- `input_usd_per_mtok` / `output_usd_per_mtok`: prices, used only when Hermes reports none.
- `extra_args`: appended to the default flags.
- `args`: replaces the default flags entirely, for CLI versions with different flags.
  Placeholders: `{prompt}`, `{model}`; without `{prompt}`, `--query=<prompt>` is appended.

`allowed_tools` -> Hermes toolsets: `read`/`edit` -> `file`, `bash` -> `terminal` (with
`--yolo`, since nobody is there to approve a command), `web` -> `web`. `file` covers reading and
writing, so a node with `read` but no `edit` can still write files: use a workspace you don't
mind it changing. `max_turns` -> `--max-turns`.

Cost: from the final `result` event when it carries one (`cost_usd`, or `usage`); else tokens x
the price; with neither, 0 and `meta.warnings` says so.
"""

from __future__ import annotations

import json
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


def toolsets(allowed: tuple[str, ...] | list[str]) -> list[str]:
    tools = set(allowed)
    out = []
    if tools & {"read", "edit"}:
        out.append("file")
    if "bash" in tools:
        out.append("terminal")
    if "web" in tools:
        out.append("web")
    return out


def build_argv(request: RunRequest) -> list[str]:
    p = request.params
    binary = cli_bin(p, "AGENTLINE_HERMES_BIN", "hermes")
    model = str(p["model"]) if p.get("model") else None
    if p.get("args") is not None:
        args, placed = render_args(p["args"], {"prompt": request.prompt, "model": model})
        return [binary, *args] if placed else [binary, *args, f"--query={request.prompt}"]
    # `--query=` keeps a prompt that starts with "-" from being read as a flag
    argv = [binary, "chat", f"--query={request.prompt}", "--oneshot", "--format", "stream-json"]
    if p.get("provider"):
        argv += ["--provider", str(p["provider"])]
    if p.get("base_url"):
        argv += ["--base-url", str(p["base_url"])]
    if model:
        argv += ["--model", model]
    sets = p.get("toolsets")
    chosen = [str(s) for s in sets] if sets is not None else toolsets(request.limits.allowed_tools)
    if chosen:
        argv += ["--toolsets", ",".join(chosen)]
    if "terminal" in chosen:
        argv.append("--yolo")
    if request.limits.max_turns:
        argv += ["--max-turns", str(request.limits.max_turns)]
    argv += [str(x) for x in p.get("extra_args") or ()]
    return argv


def parse_events(stdout: str) -> dict[str, Any]:
    """Fold Hermes's stream-json lines. The last one is a `result` event with `text` and
    `exit_code`; tool events whose output says a command wasn't found are kept for the
    missing-tool check. Unknown events are skipped."""
    result: dict[str, Any] | None = None
    texts: list[str] = []
    failed: list[str] = []
    errors: list[str] = []
    events = 0
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
        t = str(ev.get("type") or "")
        if t == "result":
            result = ev
        elif t in ("assistant", "message", "text") and isinstance(ev.get("text"), str):
            texts.append(ev["text"])
        elif t == "error":
            errors.append(str(ev.get("message") or ev.get("error") or "error"))
        elif "not found" in str(ev.get("output") or ""):
            failed.append(str(ev["output"]))
    usage = {"input_tokens": 0, "output_tokens": 0}
    cost = None
    text = texts[-1] if texts else ""
    exit_code = None
    if result is not None:
        text = str(result.get("text") or text)
        exit_code = result.get("exit_code")
        u = result.get("usage")
        if isinstance(u, dict):
            usage["input_tokens"] = int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
            usage["output_tokens"] = int(u.get("output_tokens") or u.get("completion_tokens") or 0)
        for key in ("cost_usd", "total_cost_usd", "cost"):
            if isinstance(result.get(key), int | float):
                cost = float(result[key])
                break
        if result.get("is_error") or result.get("error"):
            errors.append(str(result.get("error") or text or "error"))
    return {
        "events": events,
        "result": text,
        "exit_code": exit_code,
        "failed_commands": failed,
        "errors": errors,
        "usage": usage,
        "cost": cost,
        "session_id": result.get("session_id") if result else None,
    }


class HermesRunner:
    name = "hermes"

    def __init__(self, pricing: dict[str, Any] | None = None) -> None:
        self.pricing = dict(pricing or {})

    async def run(self, request: RunRequest) -> RunResult:
        budget = request.limits.budget_usd
        if budget is not None and budget <= 0:
            raise BudgetExceeded(0.0, 0.0, "call")
        p = request.params
        argv = build_argv(request)
        try:
            code, out, err = await exec_cli(request, argv, env_names(p, DEFAULT_ENV), "hermes")
        except BinaryMissingInWorkspace as e:
            return RunResult(None, e.output, missing_tools=[e.tool])
        transcript = out + (f"\n--- stderr ---\n{err}" if err.strip() else "")
        data = parse_events(out)
        if data["events"] == 0:
            tail = (err or out).strip()[-500:]
            raise RunnerError(f"hermes exited {code} without JSON events: {tail}")

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
                    f"hermes reported no cost and no price is known for {model!r};"
                    " recorded as 0 (set [pricing.<model>] or input/output_usd_per_mtok)"
                ]
            else:
                cost = cost_of(data["usage"], price)
        text = data["result"]
        missing = detect_missing_tools(text, *data["failed_commands"])
        if missing:
            return RunResult(None, transcript, cost, missing_tools=missing, meta=meta)
        failed = code != 0 or data["exit_code"] not in (None, 0)
        if data["errors"] or failed:
            why = "; ".join(data["errors"]) or f"exit {data['exit_code'] or code}"
            raise RunnerError(
                f"hermes reported an error ({why[-300:]})",
                cost_usd=cost,
                transcript=transcript,
                meta=meta,
            )
        if budget is not None and cost > budget:
            meta["budget_exceeded"] = {"cost_usd": cost, "budget_usd": budget}
        ok, value = parse_json_text(text)
        return RunResult(
            output=value if ok else text, transcript=transcript, cost_usd=cost, meta=meta
        )
