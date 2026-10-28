from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from demo.fakes.server import make_server

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fakes_url(tmp_path: Path) -> Iterator[str]:
    server = make_server(tmp_path / "fakes.db", llm_latency=(0.0, 0.0), pr_latency=(0.0, 0.0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
