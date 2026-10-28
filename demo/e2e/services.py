"""Fake Jira + GitHub REST APIs in their own process (never killed by the chaos driver).

Reuses the Phase 4 test fakes (`tests/actions/fakes.py`) and adds `/_control/*` endpoints the
driver uses to seed tickets, submit reviews and read back what happened:

    POST /_control/issue   {"key", "summary", "description"}
    POST /_control/review  {"repo", "number", "state", "commit_id", "body"} -> review
    GET  /_control/state   -> {"prs": {...}, "pr_creates", "reviews": [...], "comments": {...}}

    python -m demo.e2e.services --port-file PATH
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tests.actions.fakes import FakeState, Handler, _Server, jira_issue


class ControlHandler(Handler):
    def _handle(self, method: str) -> None:
        url = urlparse(self.path)
        if not url.path.startswith("/_control/"):
            return super()._handle(method)
        length = int(self.headers.get("Content-Length", 0))
        body: Any = json.loads(self.rfile.read(length)) if length else {}
        st = self.state
        if url.path == "/_control/issue" and method == "POST":
            issue = jira_issue(
                body["key"],
                body.get("summary", "Login broken"),
                body.get("description", "Login fails with an empty password."),
            )
            with st.lock:
                st.issues[body["key"]] = issue
            return self.send(201, issue)
        if url.path == "/_control/review" and method == "POST":
            review = st.add_review(
                body["repo"],
                int(body["number"]),
                body["state"],
                commit_id=body.get("commit_id"),
                body=body.get("body", ""),
                reviewer=body.get("reviewer", "alice"),
            )
            return self.send(201, review)
        if url.path == "/_control/state" and method == "GET":
            with st.lock:
                state = {
                    "prs": st.prs,
                    "pr_creates": st.pr_creates,
                    "reviews": [
                        {"repo": repo, "number": n, **r}
                        for (repo, n), rs in st.reviews.items()
                        for r in rs
                    ],
                    "comments": st.comments,
                    "comment_creates": st.comment_creates,
                }
            return self.send(200, state)
        return self.send(404, {"message": "unknown control endpoint"})


def serve(port_file: Path, port: int = 0) -> None:
    state = FakeState()
    handler = type("H", (ControlHandler,), {"state": state, "base": ""})
    server = _Server(("127.0.0.1", port), handler)
    handler.base = f"http://127.0.0.1:{server.server_address[1]}"
    port_file.write_text(str(server.server_address[1]))
    try:
        server.serve_forever(0.02)
    finally:
        server.server_close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port-file", type=Path, required=True)
    p.add_argument("--port", type=int, default=0)
    a = p.parse_args()
    serve(a.port_file, a.port)


if __name__ == "__main__":
    main()
