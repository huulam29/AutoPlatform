"""A fake `aider` CLI for tests. Behaves like `aider --message <prompt> --yes-always ...`.

The mode is picked by a `mode:<name>` word in the message. If `FAKE_AGENT_LOG` is set (it
must be declared in the runner's `env:`), the argv, cwd and env var names are written there.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time


def main() -> int:
    args = sys.argv[1:]
    prompt = args[args.index("--message") + 1] if "--message" in args else ""
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
    print("Aider v0.86.0")
    print("Main model: gpt-5 with diff edit format")
    print("Git repo: .git with 12 files")
    print()
    if mode == "sleep":
        time.sleep(30)
    if mode == "autherror":
        print("litellm.AuthenticationError: OpenAIException - invalid api key")
        return 0
    if mode == "crash":
        print("Traceback (most recent call last):", file=sys.stderr)
        print("RuntimeError: boom", file=sys.stderr)
        return 1
    if mode == "missing":
        print("Running psql -c 'select 1'")
        print("sh: 1: psql: not found")
    if mode == "text":
        print("I changed the login handler.")
    else:
        print('Done.\n```json\n{"summary": "done", "files": ["a.py"]}\n```')
    if mode != "nocost":
        print("Tokens: 2.3k sent, 120 received. Cost: $0.01 message, $0.01 session.")
        print("Tokens: 3.1k sent, 1.2k cache hit, 80 received. Cost: $0.02 message, $0.03 session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
