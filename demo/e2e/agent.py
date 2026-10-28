"""Scripted fake agents for the e2e pipeline (runner `python`).

`plan` returns a plan; `implement` makes a real commit in the workspace worktree, so the
branch, push, PR and deploy all carry real git shas. Iteration 2 addresses the review.
Both are safe to re-run after a crash: a re-run rewrites the same file and commits only if
something changed.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from agentline.registry import RunRequest, RunResult

GIT_ENV = {
    "GIT_AUTHOR_NAME": "agentline-e2e",
    "GIT_AUTHOR_EMAIL": "e2e@example.com",
    "GIT_COMMITTER_NAME": "agentline-e2e",
    "GIT_COMMITTER_EMAIL": "e2e@example.com",
}


def plan(req: RunRequest) -> RunResult:
    key = req.inputs["ticket"]["key"]
    output = {
        "summary": f"Fix {key}",
        "approach": "Reject empty passwords before hashing.",
        "steps": ["validate input", "add a test"],
        "files": [f"src/{key}.py"],
    }
    return RunResult(output=output, cost_usd=0.01, transcript=f"planned {key}")


def _git(cwd: str, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def implement(req: RunRequest) -> RunResult:
    assert req.workspace is not None, "implement needs a workspace"
    cwd = str(req.workspace.path)
    key = req.inputs["ticket"]["key"]
    review: dict[str, Any] | None = req.inputs.get("review")
    lines = [
        f"# {key}: {req.inputs['plan']['summary']}",
        "def check(password: str) -> bool:",
        "    return bool(password)",
    ]
    if review:
        notes = [
            c.get("body", "") if isinstance(c, dict) else str(c) for c in review.get("comments", [])
        ]
        lines.append("# review addressed: " + ("; ".join(notes) or review.get("state", "")))
        lines.append("def test_empty_password() -> None:\n    assert not check('')")
    os.makedirs(os.path.join(cwd, "src"), exist_ok=True)
    with open(os.path.join(cwd, "src", f"{key}.py"), "w") as f:
        f.write("\n".join(lines) + "\n")
    _git(cwd, "add", "-A")
    if _git(cwd, "status", "--porcelain"):
        _git(cwd, "commit", "-q", "-m", f"{key}: implement (iteration {req.iteration})")
    return RunResult(
        output={"summary": f"implemented {key} v{req.iteration}"},
        cost_usd=0.05,
        transcript=f"implemented {key} iteration {req.iteration}",
    )
