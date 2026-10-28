from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agentline.ledger.base import Effect, Event, Execution, Ledger, LedgerTx, Run
from agentline.ledger.sqlite import SqliteLedger

if TYPE_CHECKING:
    from agentline.ledger.postgres import PostgresLedger

POSTGRES_SCHEMES = ("postgresql://", "postgres://")


def is_postgres_url(url: str) -> bool:
    return url.startswith(POSTGRES_SCHEMES)


def sqlite_path(url: str | Path) -> Path | None:
    """The file path of a SQLite ledger URL or plain path; None for any other URL.

    `sqlite:///rel/path.db` is relative, `sqlite:////abs/path.db` absolute (the SQLAlchemy
    convention); a plain path (with `~` expanded) is used as is."""
    if isinstance(url, Path):
        return url.expanduser()
    if url.startswith("sqlite://"):
        rest = url[len("sqlite://") :]
        if not rest.startswith("/") or rest == "/":
            raise ValueError(f"bad SQLite URL {url!r}: use sqlite:///relative or sqlite:////abs")
        return Path(rest[1:]).expanduser()
    if "://" in url:
        return None
    return Path(url).expanduser()


def split_schema(url: str) -> tuple[str, str | None]:
    """`postgresql://...?schema=name` -> (the URL without `schema`, "name"). `schema` is ours,
    not libpq's: it puts the ledger's tables in that Postgres schema."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    schemas = [v for k, v in query if k == "schema"]
    rest = urlencode([(k, v) for k, v in query if k != "schema"])
    return urlunsplit(parts._replace(query=rest)), (schemas[-1] or None) if schemas else None


def ledger_from_url(url: str | Path) -> SqliteLedger | PostgresLedger:
    """`sqlite:///path`, a plain path, or `postgresql://...[?schema=name]` (needs the
    `postgres` extra)."""
    if isinstance(url, str) and is_postgres_url(url):
        from agentline.ledger.postgres import PostgresLedger

        dsn, schema = split_schema(url)
        return PostgresLedger(dsn, schema=schema)
    path = sqlite_path(url)
    if path is None:
        raise ValueError(
            f"unsupported ledger URL {url!r}: use a path, sqlite:///path or postgresql://..."
        )
    return SqliteLedger(path)


__all__ = [
    "Effect",
    "Event",
    "Execution",
    "Ledger",
    "LedgerTx",
    "Run",
    "SqliteLedger",
    "is_postgres_url",
    "ledger_from_url",
    "split_schema",
    "sqlite_path",
]
