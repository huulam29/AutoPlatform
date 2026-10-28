"""In-process fake Jira + GitHub REST servers, and small git helpers, for Phase 4 tests."""

from __future__ import annotations

import json
import os
import re
import socketserver
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

GITHUB_TOKEN = "ghp_faketoken_0123456789"
JIRA_TOKEN = "jira-fake-token-abcdef"
JIRA_EMAIL = "bot@example.com"


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:  # skip socket.getfqdn() (slow on macOS)
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)


@dataclass
class FakeState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    # github
    prs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # repo -> PRs
    pr_creates: int = 0
    merges: int = 0
    reviews: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    review_comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    fail_next_pr_create_after_write: bool = False
    # jira
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    comment_creates: int = 0
    fail_next_comment_after_write: bool = False
    # both
    requests: list[tuple[str, str]] = field(default_factory=list)
    auth_failures: int = 0

    def add_review(
        self,
        repo: str,
        number: int,
        state: str,
        *,
        commit_id: str | None = None,
        body: str = "",
        reviewer: str = "alice",
        comments: list[dict[str, Any]] | None = None,
        submitted_at: str = "2099-01-01T00:00:00Z",
    ) -> dict[str, Any]:
        with self.lock:
            rid = 1000 + sum(len(v) for v in self.reviews.values())
            review = {
                "id": rid,
                "state": state.upper(),
                "body": body,
                "user": {"login": reviewer},
                "commit_id": commit_id,
                "submitted_at": submitted_at,
            }
            self.reviews.setdefault((repo, number), []).append(review)
            self.review_comments[rid] = [
                {"pull_request_review_id": rid, **c} for c in (comments or [])
            ]
            return review


def _github(h: Handler, method: str, url: Any, body: Any) -> None:
    st = h.state
    if h.headers.get("Authorization") != f"Bearer {GITHUB_TOKEN}":
        st.auth_failures += 1
        # a hostile server echoing the credential back: it must be redacted client-side
        return h.send(401, {"message": f"Bad credentials: {h.headers.get('Authorization')}"})
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    m = re.fullmatch(
        r"/repos/([^/]+)/([^/]+)/pulls(?:/(\d+))?(/reviews|/comments|/merge)?", url.path
    )
    m2 = re.fullmatch(r"/repos/([^/]+)/([^/]+)/pulls/(\d+)/reviews/(\d+)/comments", url.path)
    if m2:
        return h.send(200, st.review_comments.get(int(m2.group(4)), []))
    if not m:
        return h.send(404, {"message": "Not Found"})
    repo, number, sub = f"{m.group(1)}/{m.group(2)}", m.group(3), m.group(4)
    with st.lock:
        prs = st.prs.setdefault(repo, [])
        if number is None and method == "GET":
            out = [p for p in prs if q.get("state", "open") in ("all", p["state"])]
            if "head" in q:
                owner, _, branch = q["head"].partition(":")
                out = [p for p in out if p["head"]["ref"] == branch and owner == m.group(1)]
            return h.send(200, out)
        if number is None and method == "POST":
            if any(p["head"]["ref"] == body["head"] and p["state"] == "open" for p in prs):
                return h.send(422, {"message": "A pull request already exists"})
            st.pr_creates += 1
            n = len(prs) + 1
            pr = {
                "number": n,
                "html_url": f"https://github.example/{repo}/pull/{n}",
                "state": "open",
                "title": body["title"],
                "body": body.get("body", ""),
                "head": {"ref": body["head"], "sha": "fakesha"},
                "base": {"ref": body["base"]},
            }
            prs.append(pr)
            if st.fail_next_pr_create_after_write:  # created, but the response is lost
                st.fail_next_pr_create_after_write = False
                return h.send(502, {"message": "Bad gateway"})
            return h.send(201, pr)
        pr = next((p for p in prs if p["number"] == int(number or 0)), None)
        if pr is None:
            return h.send(404, {"message": "Not Found"})
        if sub is None:
            return h.send(200, pr)
        if sub == "/merge" and method == "PUT":
            if pr.get("merged"):
                return h.send(405, {"message": "Pull Request is not mergeable"})
            if body.get("sha") and body["sha"] != pr["head"]["sha"]:
                return h.send(409, {"message": "Head branch was modified"})
            st.merges += 1
            pr.update(merged=True, state="closed", merge_commit_sha=f"merged{st.merges}")
            return h.send(200, {"merged": True, "sha": pr["merge_commit_sha"]})
        if sub == "/reviews":  # paginated 2 per page to exercise Link: rel="next"
            items = st.reviews.get((repo, int(number or 0)), [])
            page = int(q.get("page", "1"))
            chunk = items[(page - 1) * 2 : page * 2]
            link = None
            if page * 2 < len(items):
                link = f'<{h.base}{url.path}?per_page=100&page={page + 1}>; rel="next"'
            return h.send(200, chunk, link=link)
        all_comments = [
            c
            for r in st.reviews.get((repo, int(number or 0)), [])
            for c in st.review_comments.get(r["id"], [])
        ]
        return h.send(200, all_comments)


def _adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]}
            for p in text.split("\n\n")
        ],
    }


def _jira(h: Handler, method: str, url: Any, body: Any) -> None:
    import base64

    st = h.state
    want = "Basic " + base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
    if h.headers.get("Authorization") != want:
        st.auth_failures += 1
        return h.send(401, {"errorMessages": [f"auth failed for {h.headers.get('Authorization')}"]})
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    m = re.fullmatch(r"/rest/api/3/issue/([^/]+)(/comment)?", url.path)
    if not m:
        return h.send(404, {"errorMessages": ["not found"]})
    key, sub = m.group(1), m.group(2)
    with st.lock:
        if key not in st.issues:
            return h.send(404, {"errorMessages": ["Issue does not exist"]})
        if sub is None:
            return h.send(200, st.issues[key])
        comments = st.comments.setdefault(key, [])
        if method == "POST":
            st.comment_creates += 1
            c = {
                "id": str(10000 + st.comment_creates),
                "body": body["body"],
                "author": {"accountId": "bot", "displayName": "Bot"},
            }
            comments.append(c)
            if st.fail_next_comment_after_write:
                st.fail_next_comment_after_write = False
                return h.send(503, {"errorMessages": ["try later"]})
            return h.send(201, c)
        start, size = int(q.get("startAt", 0)), min(int(q.get("maxResults", 50)), 2)
        return h.send(
            200,
            {
                "startAt": start,
                "maxResults": size,
                "total": len(comments),
                "comments": comments[start : start + size],
            },
        )


class Handler(BaseHTTPRequestHandler):
    state: FakeState
    base: str

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send(self, status: int, body: Any, link: str | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if link:
            self.send_header("Link", link)
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else None
        self.state.requests.append((method, self.path))
        if url.path.startswith("/rest/api/"):
            _jira(self, method, url, body)
        else:
            _github(self, method, url, body)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")


@contextmanager
def fake_server() -> Iterator[tuple[str, FakeState]]:
    state = FakeState()
    handler = type("H", (Handler,), {"state": state, "base": ""})
    server = _Server(("127.0.0.1", 0), handler)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    handler.base = url
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield url, state
    finally:
        server.shutdown()
        server.server_close()


def jira_issue(key: str, summary: str = "Login broken", description: str = "It fails.") -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": _adf(description),
            "assignee": {"accountId": "acc-1", "displayName": "Dev One"},
            "status": {"name": "In Progress"},
            "labels": ["backend"],
            "project": {"key": key.split("-")[0]},
            "issuetype": {"name": "Bug"},
        },
    }


# -- git ---------------------------------------------------------------------------------
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(*args: str, cwd: Path | None = None) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@dataclass
class Repos:
    remote: Path  # bare "GitHub" remote the action pushes to
    mirror: Path  # where the agent's commits live (Phase 3: the bare mirror)

    def commit(self, name: str, content: str, branch: str = "feature") -> str:
        cur = git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.mirror)
        if cur != branch:
            git("checkout", "-q", "-B", branch, cwd=self.mirror)
        (self.mirror / name).write_text(content)
        git("add", name, cwd=self.mirror)
        git("commit", "-q", "-m", f"add {name}", cwd=self.mirror)
        return git("rev-parse", "HEAD", cwd=self.mirror)

    def remote_sha(self, branch: str) -> str | None:
        try:
            return git("rev-parse", f"refs/heads/{branch}", cwd=self.remote)
        except subprocess.CalledProcessError:
            return None


def make_repos(tmp: Path) -> Repos:
    remote, mirror = tmp / "remote.git", tmp / "mirror"
    git("init", "-q", "--bare", "-b", "main", str(remote))
    git("init", "-q", "-b", "main", str(mirror))
    (mirror / "README").write_text("hi\n")
    git("add", "README", cwd=mirror)
    git("commit", "-q", "-m", "init", cwd=mirror)
    git("push", "-q", str(remote), "main", cwd=mirror)
    return Repos(remote, mirror)
