"""Ledger backends for the contract suite.

`sqlite` always runs. `postgres` runs when AGENTLINE_PG_DSN is set (e.g.
`postgresql://postgres:pw@localhost:5432/postgres`); each test gets its own Postgres schema,
dropped afterwards."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentline.ledger import SqliteLedger
from agentline.ledger.base import Ledger

PG_ENV = "AGENTLINE_PG_DSN"
BACKENDS = ["sqlite", "postgres"]


@dataclass
class Backend:
    name: str
    factory: Callable[[], Ledger]
    opened: list[Ledger] = field(default_factory=list)

    def open(self) -> Ledger:
        """A new ledger instance (own connection) on the same store."""
        ledger = self.factory()
        self.opened.append(ledger)
        return ledger


def _sqlite(tmp_path: Path) -> Iterator[Backend]:
    path = tmp_path / "ledger.db"
    yield Backend("sqlite", lambda: SqliteLedger(path))


def _postgres() -> Iterator[Backend]:
    dsn = os.environ.get(PG_ENV)
    if not dsn:
        pytest.skip(f"postgres backend: set {PG_ENV}=postgresql://... to run it")
    psycopg = pytest.importorskip("psycopg", reason="postgres backend needs the postgres extra")
    from agentline.ledger.postgres import PostgresLedger

    schema = f"agentline_test_{uuid.uuid4().hex[:12]}"
    yield Backend("postgres", lambda: PostgresLedger(dsn, schema=schema))
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                psycopg.sql.Identifier(schema)
            )
        )


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    gen: Iterator[Backend] = _sqlite(tmp_path) if request.param == "sqlite" else _postgres()
    b = next(gen)
    try:
        yield b
    finally:
        for ledger in b.opened:
            ledger.close()
        next(gen, None)  # teardown


@pytest.fixture
def ledger(backend: Backend) -> Ledger:
    return backend.open()
