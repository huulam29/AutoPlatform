"""A ledger created before Phase 5 (no artifact tables) is upgraded in place on open."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from agentline import App, SqliteLedger, StepContext
from agentline.ledger.sqlite import SCHEMA


def old_schema() -> str:
    """The Phase 2 schema: today's minus the artifacts / artifact_links tables."""
    keep = []
    for stmt in SCHEMA.split(";"):
        if "artifact" not in stmt and stmt.strip():
            keep.append(stmt)
    return ";".join(keep) + ";"


def tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def make_app(path: Path, tmp_path: Path) -> App:
    app = App(SqliteLedger(path), artifact_root=tmp_path / "artifacts")
    flow = app.flow("f")

    @flow.step("a")
    async def a(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        return {"v": 1}

    @flow.step("b", after="a")
    async def b(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        return {"v": inputs["a"]["v"] + 1}

    return app


def test_existing_db_gets_the_new_tables(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(old_schema())
    # a finished Phase 2 execution whose result is a bare blob (no artifact row)
    conn.execute(
        "INSERT INTO runs (id, flow, flow_hash, payload_json, status, created_at, updated_at)"
        " VALUES ('r1', 'f', 'h', 'null', 'done', 0, 0)"
    )
    conn.execute(
        "INSERT INTO blobs (ref, data, size, created_at) VALUES ('legacy', ?, 9, 0)",
        (b'{"old":1}',),
    )
    conn.execute(
        "INSERT INTO executions (id, run_id, node_id, iteration, status, result_ref, created_at,"
        " finished_at) VALUES ('x1', 'r1', 'a', 1, 'done', 'legacy', 0, 0)"
    )
    conn.commit()
    conn.close()
    assert "artifacts" not in tables(path)

    app = make_app(path, tmp_path)
    assert {"artifacts", "artifact_links"} <= tables(path)
    assert app.result("r1", "a") == {"old": 1}  # old refs still read
    assert app.lineage("r1")[0]["writes"] == []

    sub = app.submit_sync("f", None)
    asyncio.run(app.worker().run_until_idle())
    assert app.result(sub.run_id, "b") == {"v": 2}
    lin = {e["node"]: e for e in app.lineage(sub.run_id)}
    assert [r["from"] for r in lin["b#1"]["reads"]] == ["a#1"]
