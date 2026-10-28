"""A fake `claude` CLI for tests. Behaves like `claude -p <prompt> --output-format json`.

The mode is picked by a `mode:<name>` word in the prompt. If `FAKE_CLAUDE_LOG` is set (it
must be declared in the runner's `env:` to get through), the argv, cwd and env var names are
written there as JSON.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time


def _mcp(args: list[str]) -> object:
    """The --mcp-config file's JSON and mode (it's deleted after the run)."""
    if "--mcp-config" not in args:
        return None
    path = args[args.index("--mcp-config") + 1]
    with open(path) as f:
        return {"data": json.load(f), "mode": oct(os.stat(path).st_mode & 0o777)}


def main() -> int:
    args = sys.argv[1:]
    prompt = args[args.index("-p") + 1] if "-p" in args else sys.stdin.read()
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "w") as f:
            json.dump(
                {
                    "argv": args,
                    "cwd": os.getcwd(),
                    "env_keys": sorted(os.environ),
                    "has_api_key": "ANTHROPIC_API_KEY" in os.environ,
                    "mcp_config": _mcp(args),
                },
                f,
            )
    m = re.search(r"mode:(\w+)", prompt)
    mode = m.group(1) if m else "json"
    result: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1234,
        "num_turns": 3,
        "result": '```json\n{"summary": "done", "files": ["a.py"]}\n```',
        "session_id": "sess-123",
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 100}},
    }
    code = 0
    if mode == "text":
        result["result"] = "I changed the login handler."
    elif mode == "error":
        result.update(subtype="error_max_turns", is_error=True, result="ran out of turns")
        code = 1
    elif mode == "missing":
        result["result"] = (
            "Tried to run the migration test.\n"
            "bash: line 1: psql: command not found\n"
            "MISSING_TOOL: protoc\n"
        )
    elif mode == "expensive":
        result["total_cost_usd"] = 5.0
    elif mode == "garbage":
        print("Error: something exploded", file=sys.stderr)
        return 2
    elif mode == "sleep":
        time.sleep(30)
    elif mode == "verbose":
        print(json.dumps([{"type": "system", "subtype": "init"}, result]))
        return 0
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    sys.exit(main())
