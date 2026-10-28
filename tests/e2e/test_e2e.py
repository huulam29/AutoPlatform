"""v0.1 acceptance: the full pipeline through real processes, webhooks, git and the CLI,
with `agentline serve` SIGKILLed along the way (see demo/e2e/run.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from demo.e2e.run import main

pytest.importorskip("uvicorn", reason="needs the server extra: uv sync --extra server")


@pytest.mark.slow
def test_ticket_to_eod_end_to_end_under_chaos(tmp_path: Path) -> None:
    argv = [
        "--tickets", "3", "--seed", "7", "--kill-min", "0.3", "--kill-max", "0.9",
        "--max-kills", "6", "--max-downtime", "0.8", "--timeout", "180",
        "--out", str(tmp_path), "--expect-clean",
    ]  # fmt: skip
    assert main(argv) == 0
