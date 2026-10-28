"""A fake `codex` CLI for tests. Behaves like `codex exec --json ... -- <prompt>`.

The mode is picked by a `mode:<name>` word in the prompt (the last argv item). If
`FAKE_AGENT_LOG` is set (it must be declared in the runner's `env:` to get through), the
argv, cwd and env var names are written there as JSON.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time


def emit(ev: dict[str, object]) -> None:
    print(json.dumps(ev))


def main() -> int:
    args = sys.argv[1:]
    prompt = args[-1] if args else ""
    log = os.environ.get("FAKE_AGENT_LOG")
    if log:
        with open(log, "w") as f:
            json.dump(
                {
                    "argv": args,
                    "cwd": os.getcwd(),
                    "env_keys": sorted(os.environ),
                    "has_api_key": "OPENAI_API_KEY" in os.environ,
                },
                f,
            )
    m = re.search(r"mode:(\w+)", prompt)
    mode = m.group(1) if m else "json"
    if mode == "garbage":
        print("Error: unknown argument --json", file=sys.stderr)
        return 2
    if mode == "legacy":
        emit({"id": "0", "msg": {"type": "task_started"}})
        emit({"id": "0", "msg": {"type": "agent_message", "message": "legacy ok"}})
        emit(
            {"id": "0", "msg": {"type": "token_count", "input_tokens": 1000, "output_tokens": 100}}
        )
        return 0
    if mode == "sleep":
        time.sleep(30)
    emit({"type": "thread.started", "thread_id": "th-1"})
    emit({"type": "turn.started"})
    if mode == "missing":
        emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "i1",
                    "type": "command_execution",
                    "command": "psql -c 'select 1'",
                    "aggregated_output": "bash: line 1: psql: command not found\n",
                    "exit_code": 127,
                    "status": "failed",
                },
            }
        )
        text = "I could not run the migration test.\nMISSING_TOOL: protoc"
    elif mode == "text":
        text = "I changed the login handler."
    else:
        text = '```json\n{"summary": "done", "files": ["a.py"]}\n```'
    if mode == "error":
        emit({"type": "turn.failed", "error": {"message": "stream disconnected"}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 2000, "output_tokens": 0}})
        return 1
    emit({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": text}})
    tokens_in = 50_000_000 if mode == "expensive" else 1000
    emit(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": tokens_in, "cached_input_tokens": 200, "output_tokens": 100},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
