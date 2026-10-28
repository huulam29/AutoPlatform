"""SQLite job queue with an SQS-style visibility timeout.

A claimed job becomes invisible for `visibility_timeout` seconds. If it isn't acked in time
(the worker crashed), it becomes visible again and is redelivered. That is at-least-once
delivery: correct about never losing a job, wrong about doing its work only once.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | claimed | done
    attempts INTEGER NOT NULL DEFAULT 0,
    visible_at REAL NOT NULL,
    created_at REAL NOT NULL,
    done_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_visible ON jobs (status, visible_at);
"""


@dataclass(frozen=True)
class Job:
    id: int
    ticket: str
    attempts: int


class JobQueue:
    def __init__(self, db_path: str | Path, visibility_timeout: float = 2.0) -> None:
        self.visibility_timeout = visibility_timeout
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def enqueue(self, ticket: str) -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (ticket, visible_at, created_at) VALUES (?, ?, ?)",
                (ticket, now, now),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def claim(self) -> Job | None:
        """Atomically take the oldest visible, unfinished job and hide it."""
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT id, ticket, attempts FROM jobs "
                    "WHERE status != 'done' AND visible_at <= ? ORDER BY id LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                self._conn.execute(
                    "UPDATE jobs SET status = 'claimed', attempts = attempts + 1, "
                    "visible_at = ? WHERE id = ?",
                    (now + self.visibility_timeout, row[0]),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return Job(id=row[0], ticket=row[1], attempts=row[2] + 1)

    def ack(self, job_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = 'done', done_at = ? WHERE id = ?", (time.time(), job_id)
            )

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")
            return {status: n for status, n in rows}

    def jobs(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ticket, status, attempts FROM jobs ORDER BY id"
            ).fetchall()
        return [{"id": r[0], "ticket": r[1], "status": r[2], "attempts": r[3]} for r in rows]

    def all_done(self) -> bool:
        counts = self.counts()
        return sum(counts.values()) > 0 and set(counts) == {"done"}

    def close(self) -> None:
        self._conn.close()
