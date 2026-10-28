"""SQLite ledger (default). WAL mode; every transaction is BEGIN IMMEDIATE, so claims and commits
from several worker processes on one host serialize cleanly."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from typing import Any

from agentline.ids import content_hash
from agentline.ledger.base import (
    Artifact,
    ArtifactLink,
    AuditEntry,
    Effect,
    Event,
    Execution,
    JournalEntry,
    Run,
    RunMessage,
    TaskRecord,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    flow TEXT NOT NULL,
    flow_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    budget_usd REAL,
    cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status, created_at);

CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs (id),
    node_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    status TEXT NOT NULL,
    tokens_needed INTEGER NOT NULL DEFAULT 1,
    tokens_live INTEGER NOT NULL DEFAULT 0,
    tokens_skip INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 1,
    reclaims INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires REAL,
    lease_epoch INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    correlation_key TEXT,
    reason TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    result_ref TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    UNIQUE (run_id, node_id, iteration)
);
CREATE INDEX IF NOT EXISTS executions_claim ON executions (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS executions_run ON executions (run_id);
CREATE INDEX IF NOT EXISTS executions_corr ON executions (correlation_key);

CREATE TABLE IF NOT EXISTS loop_counts (
    run_id TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (run_id, edge_id)
);

CREATE TABLE IF NOT EXISTS effects (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT,
    result_json TEXT,
    advice_json TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_id, node_id, iteration, name)
);

CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    usd REAL NOT NULL,
    note TEXT NOT NULL,
    at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS blobs (
    ref TEXT PRIMARY KEY,
    data BLOB NOT NULL,
    size INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    summary TEXT NOT NULL,
    storage TEXT NOT NULL,
    path TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    name TEXT NOT NULL,
    hash TEXT NOT NULL,
    direction TEXT NOT NULL,
    at REAL NOT NULL,
    source_execution_id TEXT,
    UNIQUE (execution_id, name, direction, hash)
);
CREATE INDEX IF NOT EXISTS artifact_links_run ON artifact_links (run_id, id);
CREATE INDEX IF NOT EXISTS artifact_links_hash ON artifact_links (hash);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at REAL NOT NULL,
    consumed_by TEXT
);
CREATE INDEX IF NOT EXISTS events_key ON events (correlation_key, consumed_by);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    run_id TEXT,
    node_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_log_run ON audit_log (run_id, id);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    run_id TEXT NOT NULL,
    execution_id TEXT,
    node_id TEXT,
    iteration INTEGER NOT NULL DEFAULT 1,
    task INTEGER,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS journal_run ON journal (run_id, node_id, id);

CREATE TABLE IF NOT EXISTS run_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at REAL NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    via TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS run_messages_run ON run_messages (run_id, id);

CREATE TABLE IF NOT EXISTS tasks (
    execution_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    item_json TEXT,
    result_json TEXT,
    error TEXT,
    started_at REAL,
    finished_at REAL,
    PRIMARY KEY (execution_id, idx)
);
CREATE INDEX IF NOT EXISTS tasks_run ON tasks (run_id, execution_id, idx);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    source TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    received_at REAL NOT NULL,
    PRIMARY KEY (source, delivery_id)
);
"""

_EXEC_COLUMNS = [f.name for f in fields(Execution)]
_JSON_EXEC = {"detail": "detail_json"}


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


class _JournalTasks:
    """journal + tasks (shared by SqliteTx through inheritance)."""

    c: sqlite3.Connection

    def add_journal(
        self,
        run_id: str,
        kind: str,
        message: str,
        *,
        execution_id: str | None = None,
        node_id: str | None = None,
        iteration: int = 1,
        task: int | None = None,
        detail: dict[str, Any] | None = None,
        at: float | None = None,
    ) -> int:
        cur = self.c.execute(
            "INSERT INTO journal (at, run_id, execution_id, node_id, iteration, task, kind,"
            " message, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time() if at is None else at,
                run_id,
                execution_id,
                node_id,
                iteration,
                task,
                kind,
                message,
                _dumps(detail or {}),
            ),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def list_journal(
        self, run_id: str, node_id: str | None = None, limit: int = 5000
    ) -> list[JournalEntry]:
        if node_id is None:
            rows = self.c.execute(
                "SELECT * FROM journal WHERE run_id = ? ORDER BY id LIMIT ?", (run_id, limit)
            ).fetchall()
        else:
            rows = self.c.execute(
                "SELECT * FROM journal WHERE run_id = ? AND node_id = ? ORDER BY id LIMIT ?",
                (run_id, node_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(JournalEntry(**d))
        return out

    def add_run_message(
        self,
        run_id: str,
        role: str,
        text: str,
        *,
        via: str | None = None,
        detail: dict[str, Any] | None = None,
        at: float | None = None,
    ) -> int:
        cur = self.c.execute(
            "INSERT INTO run_messages (run_id, at, role, text, via, detail_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, time.time() if at is None else at, role, text, via, _dumps(detail or {})),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def list_run_messages(self, run_id: str, after: int = 0, limit: int = 1000) -> list[RunMessage]:
        rows = self.c.execute(
            "SELECT * FROM run_messages WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
            (run_id, after, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(RunMessage(**d))
        return out

    def update_run_message(self, message_id: int, detail: dict[str, Any]) -> None:
        self.c.execute(
            "UPDATE run_messages SET detail_json = ? WHERE id = ?", (_dumps(detail), message_id)
        )

    def put_task(self, task: TaskRecord) -> None:
        self.c.execute(
            "INSERT OR REPLACE INTO tasks (execution_id, idx, run_id, node_id, iteration, status,"
            " attempts, item_json, result_json, error, started_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.execution_id,
                task.idx,
                task.run_id,
                task.node_id,
                task.iteration,
                task.status,
                task.attempts,
                _dumps(task.item),
                _dumps(task.result),
                task.error,
                task.started_at,
                task.finished_at,
            ),
        )

    def list_tasks(self, run_id: str, execution_id: str | None = None) -> list[TaskRecord]:
        if execution_id is None:
            rows = self.c.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY execution_id, idx", (run_id,)
            ).fetchall()
        else:
            rows = self.c.execute(
                "SELECT * FROM tasks WHERE run_id = ? AND execution_id = ? ORDER BY idx",
                (run_id, execution_id),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["item"] = json.loads(d.pop("item_json") or "null")
            d["result"] = json.loads(d.pop("result_json") or "null")
            out.append(TaskRecord(**d))
        return out


class SqliteTx(_JournalTasks):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.c = conn

    # -- runs -----------------------------------------------------------------------------
    def _run(self, row: sqlite3.Row | None) -> Run | None:
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return Run(**d)

    def insert_run(self, run: Run) -> None:
        self.c.execute(
            "INSERT INTO runs (id, flow, flow_hash, payload_json, status, dedupe_key, budget_usd,"
            " cost_usd, error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run.id,
                run.flow,
                run.flow_hash,
                _dumps(run.payload),
                run.status,
                run.dedupe_key,
                run.budget_usd,
                run.cost_usd,
                run.error,
                run.created_at,
                run.updated_at,
            ),
        )

    def get_run(self, run_id: str) -> Run | None:
        return self._run(self.c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    def get_run_by_dedupe(self, dedupe_key: str) -> Run | None:
        row = self.c.execute("SELECT * FROM runs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
        return self._run(row)

    def update_run(self, run_id: str, **fields_: Any) -> None:
        fields_.setdefault("updated_at", time.time())
        if "payload" in fields_:
            fields_["payload_json"] = _dumps(fields_.pop("payload"))
        cols = ", ".join(f"{k} = ?" for k in fields_)
        self.c.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields_.values(), run_id))

    def list_runs(self, status: str | None = None, limit: int = 100) -> list[Run]:
        if status:
            rows = self.c.execute(
                "SELECT * FROM runs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self.c.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [r for r in (self._run(x) for x in rows.fetchall()) if r]

    def add_cost(
        self,
        run_id: str,
        execution_id: str,
        attempt: int,
        usd: float,
        note: str,
        *,
        touch: bool = True,
        on_execution: bool = True,
    ) -> float:
        self.c.execute(
            "INSERT INTO costs (run_id, execution_id, attempt, usd, note, at) VALUES (?,?,?,?,?,?)",
            (run_id, execution_id, attempt, usd, note, time.time()),
        )
        if on_execution:
            self.c.execute(
                "UPDATE executions SET cost_usd = cost_usd + ? WHERE id = ?", (usd, execution_id)
            )
        if touch:
            self.c.execute(
                "UPDATE runs SET cost_usd = cost_usd + ?, updated_at = ? WHERE id = ?",
                (usd, time.time(), run_id),
            )
        else:
            self.c.execute("UPDATE runs SET cost_usd = cost_usd + ? WHERE id = ?", (usd, run_id))
        row = self.c.execute("SELECT cost_usd FROM runs WHERE id = ?", (run_id,)).fetchone()
        return float(row[0])

    def list_costs(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.c.execute("SELECT * FROM costs WHERE run_id = ? ORDER BY id", (run_id,))
        return [dict(r) for r in rows.fetchall()]

    # -- executions -----------------------------------------------------------------------
    def _exec(self, row: sqlite3.Row | None) -> Execution | None:
        if row is None:
            return None
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json") or "{}")
        return Execution(**d)

    def insert_execution(self, ex: Execution) -> None:
        values = {k: getattr(ex, k) for k in _EXEC_COLUMNS}
        values["detail_json"] = _dumps(values.pop("detail"))
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self.c.execute(f"INSERT INTO executions ({cols}) VALUES ({marks})", tuple(values.values()))

    def get_execution(self, exec_id: str) -> Execution | None:
        row = self.c.execute("SELECT * FROM executions WHERE id = ?", (exec_id,)).fetchone()
        return self._exec(row)

    def update_execution(
        self,
        exec_id: str,
        *,
        expect_status: str | None = None,
        expect_epoch: int | None = None,
        **fields_: Any,
    ) -> bool:
        if "detail" in fields_:
            fields_["detail_json"] = _dumps(fields_.pop("detail"))
        cols = ", ".join(f"{k} = ?" for k in fields_)
        where = "id = ?"
        params: list[Any] = [*fields_.values(), exec_id]
        if expect_status is not None:
            where += " AND status = ?"
            params.append(expect_status)
        if expect_epoch is not None:
            where += " AND lease_epoch = ?"
            params.append(expect_epoch)
        cur = self.c.execute(f"UPDATE executions SET {cols} WHERE {where}", params)
        return cur.rowcount == 1

    def list_executions(self, run_id: str) -> list[Execution]:
        rows = self.c.execute(
            "SELECT * FROM executions WHERE run_id = ? ORDER BY created_at, iteration", (run_id,)
        )
        return [e for e in (self._exec(r) for r in rows.fetchall()) if e]

    def open_execution(self, run_id: str, node_id: str) -> Execution | None:
        row = self.c.execute(
            "SELECT * FROM executions WHERE run_id = ? AND node_id = ? AND status = 'pending'"
            " ORDER BY iteration DESC LIMIT 1",
            (run_id, node_id),
        ).fetchone()
        return self._exec(row)

    def max_iteration(self, run_id: str, node_id: str) -> int:
        row = self.c.execute(
            "SELECT MAX(iteration) FROM executions WHERE run_id = ? AND node_id = ?",
            (run_id, node_id),
        ).fetchone()
        return int(row[0] or 0)

    def claim_next(self, owner: str, now: float, lease_ttl: float) -> Execution | None:
        row = self.c.execute(
            "SELECT id FROM executions WHERE status IN ('ready', 'failed') AND next_attempt_at <= ?"
            " ORDER BY next_attempt_at, created_at LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            return None
        self.c.execute(
            "UPDATE executions SET status = 'running', lease_owner = ?, lease_expires = ?,"
            " lease_epoch = lease_epoch + 1, started_at = ? WHERE id = ?",
            (owner, now + lease_ttl, now, row[0]),
        )
        return self.get_execution(row[0])

    def expired_leases(self, now: float) -> list[Execution]:
        rows = self.c.execute(
            "SELECT * FROM executions WHERE status = 'running' AND lease_expires < ?", (now,)
        )
        return [e for e in (self._exec(r) for r in rows.fetchall()) if e]

    def waiting_by_key(self, correlation_key: str) -> list[Execution]:
        rows = self.c.execute(
            "SELECT * FROM executions WHERE status = 'waiting' AND correlation_key = ?",
            (correlation_key,),
        )
        return [e for e in (self._exec(r) for r in rows.fetchall()) if e]

    def count_active(self) -> int:
        row = self.c.execute(
            "SELECT COUNT(*) FROM executions WHERE status IN ('ready', 'running', 'failed')"
        ).fetchone()
        return int(row[0])

    # -- loops ----------------------------------------------------------------------------
    def loop_count(self, run_id: str, edge_id: str) -> int:
        row = self.c.execute(
            "SELECT count FROM loop_counts WHERE run_id = ? AND edge_id = ?", (run_id, edge_id)
        ).fetchone()
        return int(row[0]) if row else 0

    def inc_loop_count(self, run_id: str, edge_id: str) -> int:
        self.c.execute(
            "INSERT INTO loop_counts (run_id, edge_id, count) VALUES (?, ?, 1)"
            " ON CONFLICT (run_id, edge_id) DO UPDATE SET count = count + 1",
            (run_id, edge_id),
        )
        return self.loop_count(run_id, edge_id)

    # -- effects --------------------------------------------------------------------------
    def _effect(self, row: sqlite3.Row | None) -> Effect | None:
        if row is None:
            return None
        d = dict(row)
        result = d.pop("result_json")
        d["result"] = json.loads(result) if result is not None else None
        advice = d.pop("advice_json")
        d["advice"] = json.loads(advice) if advice else None
        return Effect(**d)

    def get_effect(self, run_id: str, node_id: str, iteration: int, name: str) -> Effect | None:
        row = self.c.execute(
            "SELECT * FROM effects WHERE run_id = ? AND node_id = ? AND iteration = ? AND name = ?",
            (run_id, node_id, iteration, name),
        ).fetchone()
        return self._effect(row)

    def put_effect(self, effect: Effect) -> None:
        self.c.execute(
            "INSERT INTO effects (run_id, node_id, iteration, name, status, idempotency_key,"
            " result_json, advice_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (run_id, node_id, iteration, name) DO UPDATE SET"
            " status = excluded.status, idempotency_key = excluded.idempotency_key,"
            " result_json = excluded.result_json,"
            " advice_json = excluded.advice_json, updated_at = excluded.updated_at",
            (
                effect.run_id,
                effect.node_id,
                effect.iteration,
                effect.name,
                effect.status,
                effect.idempotency_key,
                None if effect.result is None else _dumps(effect.result),
                None if effect.advice is None else _dumps(effect.advice),
                effect.updated_at or time.time(),
            ),
        )

    def list_effects(self, run_id: str) -> list[Effect]:
        rows = self.c.execute(
            "SELECT * FROM effects WHERE run_id = ? ORDER BY updated_at", (run_id,)
        ).fetchall()
        return [e for e in (self._effect(r) for r in rows) if e]

    # -- blobs ----------------------------------------------------------------------------
    def put_blob(self, data: bytes) -> str:
        ref = content_hash(data)
        self.c.execute(
            "INSERT OR IGNORE INTO blobs (ref, data, size, created_at) VALUES (?, ?, ?, ?)",
            (ref, data, len(data), time.time()),
        )
        return ref

    def get_blob(self, ref: str) -> bytes | None:
        row = self.c.execute("SELECT data FROM blobs WHERE ref = ?", (ref,)).fetchone()
        return bytes(row[0]) if row else None

    # -- artifacts ------------------------------------------------------------------------
    def put_artifact(self, artifact: Artifact) -> None:
        a = artifact
        self.c.execute(
            "INSERT OR IGNORE INTO artifacts (hash, kind, media_type, size, summary, storage,"
            " path, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                a.hash,
                a.kind,
                a.media_type,
                a.size,
                a.summary,
                a.storage,
                a.path,
                a.created_at or time.time(),
            ),
        )

    def get_artifact(self, hash: str) -> Artifact | None:
        row = self.c.execute("SELECT * FROM artifacts WHERE hash = ?", (hash,)).fetchone()
        return Artifact(**dict(row)) if row else None

    def add_artifact_link(self, link: ArtifactLink) -> None:
        k = link
        self.c.execute(
            "INSERT OR IGNORE INTO artifact_links (run_id, execution_id, node_id, iteration,"
            " name, hash, direction, at, source_execution_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                k.run_id,
                k.execution_id,
                k.node_id,
                k.iteration,
                k.name,
                k.hash,
                k.direction,
                k.at or time.time(),
                k.source_execution_id,
            ),
        )

    def list_artifact_links(self, run_id: str) -> list[ArtifactLink]:
        rows = self.c.execute(
            "SELECT run_id, execution_id, node_id, iteration, name, hash, direction, at,"
            " source_execution_id FROM artifact_links WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [ArtifactLink(**dict(r)) for r in rows]

    # -- events ---------------------------------------------------------------------------
    def insert_event(self, correlation_key: str, payload: Any, now: float) -> int:
        cur = self.c.execute(
            "INSERT INTO events (correlation_key, payload_json, received_at) VALUES (?, ?, ?)",
            (correlation_key, _dumps(payload), now),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def unconsumed_event(self, correlation_key: str, since: float = 0.0) -> Event | None:
        row = self.c.execute(
            "SELECT * FROM events WHERE correlation_key = ? AND consumed_by IS NULL"
            " AND received_at >= ? ORDER BY id LIMIT 1",
            (correlation_key, since),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return Event(**d)

    def consume_event(self, event_id: int, consumer: str) -> None:
        self.c.execute("UPDATE events SET consumed_by = ? WHERE id = ?", (consumer, event_id))

    # -- deliveries -----------------------------------------------------------------------
    def claim_delivery(self, source: str, delivery_id: str, now: float) -> bool:
        cur = self.c.execute(
            "INSERT OR IGNORE INTO webhook_deliveries (source, delivery_id, received_at)"
            " VALUES (?, ?, ?)",
            (source, delivery_id, now),
        )
        return cur.rowcount == 1

    # -- audit ----------------------------------------------------------------------------
    def add_audit(
        self,
        actor: str,
        action: str,
        run_id: str | None = None,
        node_id: str | None = None,
        detail: dict[str, Any] | None = None,
        at: float | None = None,
    ) -> int:
        cur = self.c.execute(
            "INSERT INTO audit_log (at, actor, action, run_id, node_id, detail_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                time.time() if at is None else at,
                actor,
                action,
                run_id,
                node_id,
                _dumps(detail or {}),
            ),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def list_audit(self, run_id: str | None = None, limit: int = 1000) -> list[AuditEntry]:
        if run_id is None:
            rows = self.c.execute(
                "SELECT * FROM (SELECT * FROM audit_log ORDER BY id DESC LIMIT ?) ORDER BY id",
                (limit,),
            ).fetchall()
        else:
            rows = self.c.execute(
                "SELECT * FROM audit_log WHERE run_id = ? ORDER BY id LIMIT ?", (run_id, limit)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(AuditEntry(**d))
        return out


class SqliteLedger:
    def __init__(self, path: str | Path, timeout: float = 30.0) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=timeout
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[SqliteTx]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield SqliteTx(self._conn)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()
