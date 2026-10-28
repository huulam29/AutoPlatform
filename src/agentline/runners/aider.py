"""Runner `aider`: Aider in one-shot scripting mode (`aider --message <prompt> --yes-always`).

`with:` params:
- `bin`: the CLI (else `$AGENTLINE_AIDER_BIN`, else `aider`).
- `model`: passed as `--model` (any model name aider/litellm knows).
- `env`: host env var names to pass (default `[OPENAI_API_KEY, ANTHROPIC_API_KEY]`).
  Nothing else from the host env reaches the CLI except PATH/HOME/locale basics locally.
- `auto_commits: true`: let aider commit each edit itself (default off, see below).
- `extra_args`: appended to the default flags.
- `args`: replaces the default flags entirely, for aider versions with different flags.
  Placeholders: `{prompt}`, `{model}`; without `{prompt}`, `--message <prompt>` is appended.
- `exclude_aider_files` (default true): in a workspace, add `.aider*` to the repo's
  `info/exclude` first, so aider's caches are never committed by `finalize()`.

Commits: `--no-auto-commits --no-dirty-commits` by default. The workspace's `finalize()`
already commits whatever the agent leaves behind, as one commit per node iteration with
agentline's identity, and its diff and `base_sha` don't depend on how many commits there are.
`auto_commits: true` gives aider's per-edit commits instead (finalize then has nothing left).
`--no-gitignore` stops aider from editing the repo's `.gitignore`, and the chat/input history
files go to /dev/null so they never land in the worktree.

`allowed_tools` -> aider flags. Aider has no per-tool allowlist; what can be enforced:
- no `edit` -> `--chat-mode ask --dry-run`: aider answers without proposing edits, and
  `--dry-run` makes sure no file is written even if it tries.
- no `bash` -> `--no-suggest-shell-commands`: aider doesn't propose (so can't run) commands.
  With `bash`, aider may propose commands; whether `--yes-always` runs them depends on the
  aider version.
- no `web` -> `--no-detect-urls`: URLs in the prompt aren't fetched.
- `read` can't be restricted: aider reads the repo (repo map and the files it adds).
`max_turns` has no aider equivalent and isn't enforced; the node's `timeout` is.

Cost: parsed from aider's `Cost: $x message, $y session.` lines (the last session total).
When aider prints none (unknown model price), cost is 0 with a warning in `meta`.
Output: the last ```json block (or JSON text) in aider's output, else the text itself.
"""

from __future__ import annotations

import contextlib
import re
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
from agentline.runners.common import (
    RunnerError,
    detect_missing_tools,
    parse_json_text,
    workspace_exec,
)

DEFAULT_ENV = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
BASE_FLAGS = (
    "--yes-always",
    "--no-pretty",
    "--no-stream",
    "--no-check-update",
    "--analytics-disable",
    "--no-gitignore",
    "--chat-history-file",
    "/dev/null",
    "--input-history-file",
    "/dev/null",
)
_COST = re.compile(r"Cost: \$([0-9][0-9,]*(?:\.[0-9]+)?) message, \$([0-9][0-9,]*(?:\.[0-9]+)?)")
_TOKENS = re.compile(r"Tokens: ([0-9.]+)(k?) sent.*?, ([0-9.]+)(k?) received")
_ERRORS = re.compile(r"^.*\b(litellm\.\w+Error|AuthenticationError|APIConnectionError)\b.*$", re.M)
EXCLUDE_SCRIPT = (
    'f=$(git rev-parse --git-path info/exclude) && mkdir -p "$(dirname "$f")"'
    ' && { grep -qxF ".aider*" "$f" 2>/dev/null || echo ".aider*" >> "$f"; }'
)


def tool_flags(allowed: tuple[str, ...] | list[str]) -> list[str]:
    tools = set(allowed)
    flags: list[str] = []
    if "edit" not in tools:
        flags += ["--chat-mode", "ask", "--dry-run"]
    if "bash" not in tools:
        flags.append("--no-suggest-shell-commands")
    if "web" not in tools:
        flags.append("--no-detect-urls")
    return flags


def build_argv(request: RunRequest) -> list[str]:
    p = request.params
    binary = cli_bin(p, "AGENTLINE_AIDER_BIN", "aider")
    model = str(p["model"]) if p.get("model") else None
    if p.get("args") is not None:
        args, placed = render_args(p["args"], {"prompt": request.prompt, "model": model})
        return [binary, *args] if placed else [binary, *args, "--message", request.prompt]
    argv = [binary, "--message", request.prompt, *BASE_FLAGS]
    if p.get("auto_commits"):
        argv.append("--auto-commits")
    else:
        argv += ["--no-auto-commits", "--no-dirty-commits"]
    if model:
        argv += ["--model", model]
    argv += tool_flags(request.limits.allowed_tools)
    argv += [str(x) for x in p.get("extra_args") or ()]
    return argv


def _num(value: str, k: str) -> int:
    return int(float(value) * (1000 if k else 1))


def parse_output(stdout: str) -> dict[str, Any]:
    costs = _COST.findall(stdout)
    cost = float(costs[-1][1].replace(",", "")) if costs else None
    sent = received = 0
    for s, sk, r, rk in _TOKENS.findall(stdout):
        sent += _num(s, sk)
        received += _num(r, rk)
    text = "\n".join(
        ln for ln in stdout.splitlines() if not ln.startswith(("Tokens: ", "Cost: "))
    ).strip()
    errors = [m.group(0).strip() for m in _ERRORS.finditer(stdout)]
    return {
        "cost": cost,
        "usage": {"input_tokens": sent, "output_tokens": received},
        "text": text,
        "errors": errors,
    }


class AiderRunner:
    name = "aider"

    async def run(self, request: RunRequest) -> RunResult:
        budget = request.limits.budget_usd
        if budget is not None and budget <= 0:
            raise BudgetExceeded(0.0, 0.0, "call")
        p = request.params
        argv = build_argv(request)
        if request.workspace is not None and p.get("exclude_aider_files", True):
            # best effort: a workspace without git just doesn't get the exclude
            with contextlib.suppress(Exception):
                await workspace_exec(request.workspace, ["sh", "-c", EXCLUDE_SCRIPT], timeout=30)
        try:
            code, out, err = await exec_cli(request, argv, env_names(p, DEFAULT_ENV), "aider")
        except BinaryMissingInWorkspace as e:
            return RunResult(None, e.output, missing_tools=[e.tool])
        transcript = out + (f"\n--- stderr ---\n{err}" if err.strip() else "")
        data = parse_output(out)
        meta: dict[str, Any] = {
            "model": p.get("model"),
            "usage": data["usage"],
            "exit_code": code,
        }
        cost = data["cost"]
        if cost is None:
            cost = 0.0
            meta["warnings"] = ["aider reported no cost (unknown model price?); recorded as 0"]
        missing = detect_missing_tools(out)
        if missing:
            return RunResult(None, transcript, cost, missing_tools=missing, meta=meta)
        if code != 0 or data["errors"]:
            why = "; ".join(data["errors"]) or (err or out).strip()[-300:] or f"exit {code}"
            raise RunnerError(
                f"aider failed (exit {code}): {why[-300:]}",
                cost_usd=cost,
                transcript=transcript,
                meta=meta,
            )
        if budget is not None and cost > budget:
            meta["budget_exceeded"] = {"cost_usd": cost, "budget_usd": budget}
        ok, value = parse_json_text(data["text"])
        return RunResult(
            output=value if ok else data["text"], transcript=transcript, cost_usd=cost, meta=meta
        )
