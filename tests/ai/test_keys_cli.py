"""`agentline keys`: set from stdin, list without values, remove."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from agentline.cli import main


def test_keys_cli(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-or-secret-value-123\n"))
    assert main(["keys", "set", "OPENROUTER_API_KEY", "--stdin"]) == 0
    assert main(["keys", "list"]) == 0
    out = capsys.readouterr().out
    assert "OPENROUTER_API_KEY" in out and "set (secrets file)" in out
    assert "sk-or-secret-value-123" not in out
    assert main(["keys", "rm", "OPENROUTER_API_KEY"]) == 0
    assert main(["keys", "rm", "OPENROUTER_API_KEY"]) == 1
    assert main(["keys", "set"]) == 2
