from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.actions.conftest import Harness, fake  # noqa: F401  (fixture re-export)

GH_SECRET = "gh-webhook-secret"
JIRA_SECRET = "jira-shared-secret"


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    yield harness
    harness.ledger.close()


def sign(body: bytes, secret: str = GH_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def gh_headers(body: bytes, delivery: str, event: str = "pull_request_review") -> dict[str, str]:
    return {
        "X-Hub-Signature-256": sign(body),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }


def review_event(
    state: str, *, repo: str = "acme/app", number: int = 5, review_id: int = 1, body: str = ""
) -> bytes:
    payload: dict[str, Any] = {
        "action": "submitted",
        "review": {
            "id": review_id,
            "state": state,
            "body": body,
            "user": {"login": "alice"},
            "commit_id": "c0ffee",
            "submitted_at": "2099-01-01T00:00:00Z",
        },
        "pull_request": {"number": number},
        "repository": {"full_name": repo},
    }
    return json.dumps(payload).encode()
