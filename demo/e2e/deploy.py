"""The fake EOD environment: deploys and test runs are appended to JSON-lines logs.

    python -m demo.e2e.deploy deploy <state_dir> <branch> <sha>
    python -m demo.e2e.deploy check  <state_dir> <sha>   # prints <sha> if it was deployed
    python -m demo.e2e.deploy test   <state_dir>         # "tests pass"

The deploy sleeps briefly *after* recording, so a kill can land between the deploy taking
effect and agentline recording it: the case the `check` command exists for.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def _append(path: Path, record: dict[str, object]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()


def main(argv: list[str]) -> int:
    cmd, state = argv[0], Path(argv[1])
    deploys = state / "deploys.jsonl"
    if cmd == "deploy":
        branch, sha = argv[2], argv[3]
        _append(deploys, {"branch": branch, "sha": sha, "at": time.time()})
        time.sleep(0.3)
        print(f"deployed {sha} to eod")
        return 0
    if cmd == "check":
        sha = argv[2]
        if deploys.exists():
            for line in deploys.read_text().splitlines():
                if json.loads(line)["sha"] == sha:
                    print(sha)
                    return 0
        return 0
    if cmd == "test":
        _append(state / "tests.jsonl", {"at": time.time()})
        print("42 passed")
        return 0
    print(f"unknown command {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
