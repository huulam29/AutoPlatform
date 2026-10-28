from __future__ import annotations

import threading
from pathlib import Path

from demo.fakes.server import get_json, make_server, post_json


def test_llm_calls_are_counted_per_job(fakes_url: str) -> None:
    out = post_json(f"{fakes_url}/llm", {"job_id": "j1", "prompt": "hi"})
    assert out["cost_usd"] > 0
    post_json(f"{fakes_url}/llm", {"job_id": "j1", "prompt": "hi"})
    post_json(f"{fakes_url}/llm", {"job_id": "j2", "prompt": "hi"})
    stats = get_json(f"{fakes_url}/stats")
    assert stats["llm_calls"] == 3
    assert stats["llm_calls_by_job"] == {"j1": 2, "j2": 1}


def test_pr_creation_never_dedupes(fakes_url: str) -> None:
    a = post_json(f"{fakes_url}/prs", {"ticket": "T-1", "branch": "b1", "title": "x"})
    b = post_json(f"{fakes_url}/prs", {"ticket": "T-1", "branch": "b1", "title": "x"})
    assert a["number"] != b["number"]
    assert len(get_json(f"{fakes_url}/prs?branch=b1")) == 2
    assert get_json(f"{fakes_url}/prs?branch=other") == []
    assert get_json(f"{fakes_url}/stats")["prs_by_ticket"] == {"T-1": 2}


def test_counts_survive_restart(tmp_path: Path) -> None:
    db = tmp_path / "fakes.db"
    for _ in range(2):
        server = make_server(db, llm_latency=(0.0, 0.0), pr_latency=(0.0, 0.0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        post_json(f"{url}/prs", {"ticket": "T-1", "branch": "b1"})
        stats = get_json(f"{url}/stats")
        server.shutdown()
        server.server_close()
    assert stats["prs"] == 2
