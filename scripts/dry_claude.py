#!/usr/bin/env python3
"""A stand-in for `claude -p ... --output-format json`, to dry-run scripts/real_run.sh.

    AGENTLINE_CLAUDE_BIN="$PWD/scripts/dry_claude.py" scripts/real_run.sh

The plan prompt gets a JSON plan; the fix prompt really edits pricing.py in the current
directory (the agentline worktree), adds a test, runs the tests and commits. Costs are fake.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    prompt = args[args.index("-p") + 1] if "-p" in args else sys.stdin.read()
    if "Do not edit anything" in prompt:
        answer = json.dumps(
            {
                "summary": "Treat discount_percent as a percentage",
                "cause": "total() multiplies by discount_percent instead of discount_percent/100.",
                "steps": ["divide the discount by 100", "add a regression test"],
                "files": ["pricing.py", "test_pricing.py"],
            }
        )
        turns, cost = 2, 0.0123
    else:
        path = Path("pricing.py")
        path.write_text(
            path.read_text().replace(
                "subtotal * discount_percent", "subtotal * discount_percent / 100"
            )
        )
        with open("test_pricing.py") as f:
            tests = f.read()
        if "test_zero_items" not in tests:
            tests = tests.replace(
                'if __name__ == "__main__":',
                "class RegressionTest(unittest.TestCase):\n"
                "    def test_zero_items(self) -> None:\n"
                "        self.assertEqual(total([], 50), 0.0)\n\n\n"
                'if __name__ == "__main__":',
            )
            with open("test_pricing.py", "w") as f:
                f.write(tests)
        test = subprocess.run(
            [sys.executable, "-m", "unittest", "-q"], capture_output=True, text=True
        )
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "dry",
            "GIT_AUTHOR_EMAIL": "dry@example.com",
            "GIT_COMMITTER_NAME": "dry",
            "GIT_COMMITTER_EMAIL": "dry@example.com",
        }
        subprocess.run(["git", "add", "-A"], check=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fix: discount is a percentage"], check=True, env=env
        )
        answer = f"Fixed the discount to divide by 100 and added a test. Tests: {test.returncode}"
        turns, cost = 6, 0.0456
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": turns,
                "result": answer,
                "session_id": "dry-run",
                "total_cost_usd": cost,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
