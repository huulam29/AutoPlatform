from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentline import App, SqliteLedger


@pytest.fixture
def app(tmp_path: Path) -> Iterator[App]:
    ledger = SqliteLedger(tmp_path / "ledger.db")
    yield App(ledger, backoff_base=0.0)
    ledger.close()


def statuses(app: App, run_id: str) -> dict[str, str]:
    return {x.key: x.status for x in app.executions(run_id)}
