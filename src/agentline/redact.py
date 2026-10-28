"""Secret redaction: every known secret value is replaced by `[REDACTED:<NAME>]` before it
reaches logs, transcripts, the ledger or CLI output (ARCHITECTURE §10).

Secrets come from env vars whose names look secret (`*_TOKEN`, `*_SECRET`, `*_KEY`,
`*_PASSWORD`, plus a few well-known names) and from explicit `add(name, value)` calls. Values
shorter than `MIN_LEN` characters are ignored (too many false positives).

Matching is exact on the literal value: a secret that was transformed (base64, URL-encoded,
split across lines, hashed) is **not** caught. All values are matched with one compiled
alternation, longest first, rebuilt only when the set changes.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

MIN_LEN = 8
SECRET_SUFFIXES = ("_TOKEN", "_SECRET", "_KEY", "_PASSWORD")
SECRET_NAMES = frozenset({"ANTHROPIC_API_KEY", "GITHUB_TOKEN", "JIRA_API_TOKEN"})


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return upper in SECRET_NAMES or upper.endswith(SECRET_SUFFIXES)


class Redactor:
    def __init__(self, secrets: Mapping[str, str] | None = None, *, min_len: int = MIN_LEN):
        self.min_len = min_len
        self._by_value: dict[str, str] = {}  # value -> name
        self._pattern: re.Pattern[str] | None = None
        self._bpattern: re.Pattern[bytes] | None = None
        self._dirty = False
        for name, value in (secrets or {}).items():
            self.add(name, value)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, min_len: int = MIN_LEN) -> Redactor:
        env = os.environ if env is None else env
        return cls({k: v for k, v in env.items() if is_secret_name(k)}, min_len=min_len)

    def add(self, name: str, value: str | None) -> None:
        """Register a secret. Values shorter than `min_len` are ignored."""
        if not value or len(value) < self.min_len or value in self._by_value:
            return
        self._by_value[value] = name
        self._dirty = True

    @property
    def names(self) -> list[str]:
        return sorted(set(self._by_value.values()))

    def __bool__(self) -> bool:
        return bool(self._by_value)

    def __len__(self) -> int:
        return len(self._by_value)

    def _compile(self) -> None:
        values = sorted(self._by_value, key=len, reverse=True)
        self._pattern = re.compile("|".join(re.escape(v) for v in values)) if values else None
        self._bpattern = (
            re.compile(b"|".join(re.escape(v.encode()) for v in values)) if values else None
        )
        self._dirty = False

    def _sub(self, m: re.Match[str]) -> str:
        return f"[REDACTED:{self._by_value[m.group(0)]}]"

    def _bsub(self, m: re.Match[bytes]) -> bytes:
        return f"[REDACTED:{self._by_value[m.group(0).decode()]}]".encode()

    # -- public API -----------------------------------------------------------------------
    def redact(self, text: str) -> str:
        if not self._by_value or not text:
            return text
        if self._dirty:
            self._compile()
        assert self._pattern is not None
        return self._pattern.sub(self._sub, text)

    def redact_bytes(self, data: bytes) -> bytes:
        if not self._by_value or not data:
            return data
        if self._dirty:
            self._compile()
        assert self._bpattern is not None
        return self._bpattern.sub(self._bsub, data)

    def redact_value(self, value: Any) -> Any:
        """Walk a JSON-like value; strings (including dict keys) are redacted. Other types and
        unchanged containers are returned as they are."""
        if not self._by_value:
            return value
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {
                (self.redact(k) if isinstance(k, str) else k): self.redact_value(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(v) for v in value)
        if isinstance(value, bytes):
            return self.redact_bytes(value)
        return value

    def redact_opt(self, text: str | None) -> str | None:
        return None if text is None else self.redact(text)
