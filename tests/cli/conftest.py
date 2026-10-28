from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.cli.helpers import Cli, make_cli


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> Cli:
    return make_cli(tmp_path, monkeypatch, capsys)
