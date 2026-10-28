"""The Phase 10 real-run kit works end to end with the scripted stand-in for `claude`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.slow
def test_real_run_kit_dry_run(tmp_path: Path) -> None:
    env = {**os.environ, "AGENTLINE_CLAUDE_BIN": str(ROOT / "scripts" / "dry_claude.py")}
    out = subprocess.run(
        ["bash", str(ROOT / "scripts" / "real_run.sh"), str(tmp_path / "work")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert ": done" in out.stdout
    diff = (tmp_path / "work" / "diff.txt").read_text()
    assert "discount_percent / 100" in diff and "__pycache__" not in diff
    assert "fix: discount is a percentage" in (tmp_path / "work" / "commits.txt").read_text()
