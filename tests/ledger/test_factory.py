"""`ledger_from_url`, the CLI's --db and the chaos harness's --ledger-url (no server needed)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from agentline.cli.env import CliEnv
from agentline.ledger import SqliteLedger, ledger_from_url, split_schema, sqlite_path
from agentline.ledger import postgres as pg
from demo.chaos import parse_args, run_ledger_url


def test_sqlite_paths_and_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert sqlite_path("sqlite:///rel/x.db") == Path("rel/x.db")
    assert sqlite_path("sqlite:////abs/x.db") == Path("/abs/x.db")
    assert sqlite_path("plain.db") == Path("plain.db")
    assert sqlite_path(tmp_path / "p.db") == tmp_path / "p.db"
    assert sqlite_path("~/x.db") == Path.home() / "x.db"
    assert sqlite_path("postgresql://h/db") is None
    with pytest.raises(ValueError):
        sqlite_path("sqlite://x.db")
    for url in ("a.db", "sqlite:///b.db", f"sqlite:///{tmp_path}/c.db", tmp_path / "d.db"):
        ledger = ledger_from_url(url)
        assert isinstance(ledger, SqliteLedger)
        with ledger.tx() as tx:
            assert tx.count_active() == 0
        ledger.close()
    assert {p.name for p in tmp_path.glob("*.db")} == {"a.db", "b.db", "c.db", "d.db"}


def test_unsupported_url() -> None:
    with pytest.raises(ValueError, match="unsupported ledger URL"):
        ledger_from_url("mysql://h/db")


class FakePg:
    def __init__(self, dsn: str, *, schema: str | None = None) -> None:
        self.dsn, self.schema = dsn, schema


def test_postgres_urls_route_to_postgres_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pg, "PostgresLedger", FakePg)
    got: Any = ledger_from_url("postgresql://u:p@h:5432/db?sslmode=disable&schema=tenant_a")
    assert isinstance(got, FakePg)
    assert (got.dsn, got.schema) == ("postgresql://u:p@h:5432/db?sslmode=disable", "tenant_a")
    got = ledger_from_url("postgres://h/db")
    assert (got.dsn, got.schema) == ("postgres://h/db", None)


def test_split_schema_and_redact() -> None:
    assert split_schema("postgresql://h/db?schema=") == ("postgresql://h/db", None)
    assert pg.redact_dsn("postgresql://u:s3cret@h:5432/db") == "postgresql://u:***@h:5432/db"
    assert pg.redact_dsn("postgresql://h/db") == "postgresql://h/db"
    assert "s3cret" not in pg.redact_dsn("host=h password=s3cret dbname=x")


def test_cli_db_accepts_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENTLINE_DB", raising=False)
    env = CliEnv(argparse.Namespace(db=f"sqlite:///{tmp_path}/sub/l.db"))
    assert env.db_url == tmp_path / "sub" / "l.db" and (tmp_path / "sub").is_dir()
    assert env.db_path == tmp_path / "sub" / "l.db"

    monkeypatch.setenv("AGENTLINE_DB", "postgresql://u:s3cret@h/db")
    env = CliEnv(argparse.Namespace(db=None))
    assert env.db_url == "postgresql://u:s3cret@h/db"
    assert env.db_path == "postgresql://u:***@h/db"

    monkeypatch.setattr(pg, "PostgresLedger", FakePg)
    assert isinstance(env.app.ledger, FakePg)


def test_chaos_ledger_url_gets_a_fresh_schema(tmp_path: Path) -> None:
    cfg, _ = parse_args(["--target", "durable", "--ledger-url", "postgresql://h/db"])
    assert cfg.ledger_url == "postgresql://h/db"
    assert parse_args([])[0].ledger_url is None
    url = run_ledger_url("postgresql://h/db?sslmode=disable", tmp_path / "durable-seed1-17")
    assert url == "postgresql://h/db?sslmode=disable&schema=chaos_durable_seed1_17"
    assert run_ledger_url("postgresql://h/db?schema=mine", tmp_path) == (
        "postgresql://h/db?schema=mine"
    )
    assert run_ledger_url("/x/ledger.db", tmp_path) == "/x/ledger.db"
    assert run_ledger_url(None, tmp_path) is None
