from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any


def new_id(prefix: str = "") -> str:
    """Time-sortable id: 12 hex digits of milliseconds + 8 random hex digits."""
    return f"{prefix}{int(time.time() * 1000):012x}{secrets.token_hex(4)}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
