"""Fake GitHub + fake LLM in one stdlib HTTP server.

Runs as its own process so killing a worker never touches it. Every call is recorded in
SQLite, so the chaos harness can count duplicate PRs and repeated LLM calls afterwards.

Endpoints:
    POST /llm   {"job_id", "prompt"}           -> {"text", "cost_usd"}  (sleeps: latency)
    POST /prs   {"ticket", "branch", "title"}  -> {"number", ...}       (never dedupes)
    GET  /prs?branch=<b>                       -> [{"number", ...}]
    POST /deploys {"ticket", "env", "sha"}     -> {"id", ...}           (never dedupes)
    GET  /deploys?ticket=<t>                   -> [{"id", ...}]
    GET  /stats                                -> call counts
"""

from __future__ import annotations

import argparse
import json
import random
import socketserver
import sqlite3
import threading
import time
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deploys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT NOT NULL,
    env TEXT NOT NULL,
    sha TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS prs (
    number INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT NOT NULL,
    branch TEXT NOT NULL,
    title TEXT NOT NULL,
    at REAL NOT NULL
);
"""

LLM_COST_USD = 0.01


class FakeStore:
    """SQLite-backed record of every call. Thread-safe via a lock (requests are tiny)."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def record_llm(self, job_id: str, prompt: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO llm_calls (job_id, prompt, at) VALUES (?, ?, ?)",
                (job_id, prompt, time.time()),
            )

    def create_pr(self, ticket: str, branch: str, title: str) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO prs (ticket, branch, title, at) VALUES (?, ?, ?, ?)",
                (ticket, branch, title, time.time()),
            )
            number = cur.lastrowid
        return {"number": number, "ticket": ticket, "branch": branch, "title": title}

    def find_prs(self, branch: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT number, ticket, branch, title FROM prs WHERE branch = ? ORDER BY number",
                (branch,),
            ).fetchall()
        return [{"number": r[0], "ticket": r[1], "branch": r[2], "title": r[3]} for r in rows]

    def create_deploy(self, ticket: str, env: str, sha: str) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO deploys (ticket, env, sha, at) VALUES (?, ?, ?, ?)",
                (ticket, env, sha, time.time()),
            )
        return {"id": cur.lastrowid, "ticket": ticket, "env": env, "sha": sha}

    def find_deploys(self, ticket: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ticket, env, sha FROM deploys WHERE ticket = ? ORDER BY id", (ticket,)
            ).fetchall()
        return [{"id": r[0], "ticket": r[1], "env": r[2], "sha": r[3]} for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            llm = self._conn.execute("SELECT job_id FROM llm_calls").fetchall()
            prs = self._conn.execute("SELECT ticket FROM prs").fetchall()
            deploys = self._conn.execute("SELECT ticket FROM deploys").fetchall()
        return {
            "llm_calls": len(llm),
            "llm_calls_by_job": dict(Counter(r[0] for r in llm)),
            "prs": len(prs),
            "prs_by_ticket": dict(Counter(r[0] for r in prs)),
            "deploys": len(deploys),
            "deploys_by_ticket": dict(Counter(r[0] for r in deploys)),
        }

    def close(self) -> None:
        self._conn.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn(), which can take 30s+ on macOS.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)


def make_handler(
    store: FakeStore, llm_latency: tuple[float, float], pr_latency: tuple[float, float]
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # quiet
            pass

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            body = self._body()
            if path == "/llm":
                # Recorded on arrival: a provider bills the call even if the client dies mid-way.
                store.record_llm(str(body["job_id"]), str(body.get("prompt", "")))
                time.sleep(random.uniform(*llm_latency))
                text = f"plan for {body['job_id']}: change foo, add tests"
                self._send(200, {"text": text, "cost_usd": LLM_COST_USD})
            elif path == "/prs":
                pr = store.create_pr(
                    str(body["ticket"]), str(body["branch"]), str(body.get("title", ""))
                )
                # The PR exists before the response is sent, so a client killed while waiting
                # never learns about it (a lost response).
                time.sleep(random.uniform(*pr_latency))
                self._send(201, pr)
            elif path == "/deploys":
                dep = store.create_deploy(str(body["ticket"]), str(body["env"]), str(body["sha"]))
                time.sleep(random.uniform(*pr_latency))  # lost-response window, like /prs
                self._send(201, dep)
            else:
                self._send(404, {"error": "not found"})

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/prs":
                branch = parse_qs(url.query).get("branch", [""])[0]
                self._send(200, store.find_prs(branch))
            elif url.path == "/deploys":
                ticket = parse_qs(url.query).get("ticket", [""])[0]
                self._send(200, store.find_deploys(ticket))
            elif url.path == "/stats":
                self._send(200, store.stats())
            elif url.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

    return Handler


def make_server(
    db_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 0,
    llm_latency: tuple[float, float] = (0.05, 0.3),
    pr_latency: tuple[float, float] = (0.05, 0.2),
) -> ThreadingHTTPServer:
    store = FakeStore(db_path)
    return _Server((host, port), make_handler(store, llm_latency, pr_latency))


def get_json(url: str, timeout: float = 10.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def post_json(url: str, body: dict[str, Any], timeout: float = 10.0) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _range(text: str) -> tuple[float, float]:
    lo, hi = (float(x) for x in text.split(","))
    return lo, hi


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake GitHub + LLM server")
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file", help="write the bound port here once listening")
    parser.add_argument("--llm-latency", default="0.05,0.3", help="min,max seconds")
    parser.add_argument("--pr-latency", default="0.05,0.2", help="min,max seconds")
    args = parser.parse_args()
    server = make_server(
        args.db,
        port=args.port,
        llm_latency=_range(args.llm_latency),
        pr_latency=_range(args.pr_latency),
    )
    if args.port_file:
        Path(args.port_file).write_text(str(server.server_address[1]))
    server.serve_forever()


if __name__ == "__main__":
    main()
