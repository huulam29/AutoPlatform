"""People who use the engine API under their own name (spec 007).

`$AGENTLINE_HOME/people.json` holds, per person, a name, an admin flag, the creation time and the
SHA-256 of their current token. The token itself is shown once when it is issued and kept nowhere.
The file is owner-only (0600) and written atomically; it is re-read when it changes on disk, so a
revocation from the CLI reaches a running server on its next request.

Tokens are 256-bit random strings (`alp_...`), so a fast hash is enough: nothing can be guessed
from it. A bad file is logged and matches nobody; writes then refuse rather than overwrite it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError

log = logging.getLogger("agentline.people")

TOKEN_PREFIX = "alp_"
NAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
RESERVED = frozenset({"shared-token", "local"})


def people_path() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home.expanduser() / "people.json"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def check_person_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValidationError("a name is 1-64 letters, digits, '.', '_', '-' or '@' (no spaces)")
    if name.lower() in RESERVED:
        raise ValidationError(f"{name!r} is reserved")
    return name


@dataclass(frozen=True)
class Person:
    name: str
    admin: bool
    created_at: float
    token_sha256: str

    def public(self) -> dict[str, Any]:
        """What the API and CLI may show: never the hash."""
        return {"name": self.name, "admin": self.admin, "created_at": self.created_at}


class PeopleError(ValidationError):
    """A people change that can't be made (unknown name, taken name, unreadable file)."""


class PeopleStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or people_path()
        self._lock = threading.Lock()
        self._sig: tuple[int, int, int] | None = None
        self._people: list[Person] = []
        self._broken = False

    # -- reading ------------------------------------------------------------------------------
    def _refresh(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._sig, self._people, self._broken = None, [], False
            return
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if sig == self._sig:
            return
        try:
            data = json.loads(self.path.read_text())
            people = [
                Person(
                    name=str(p["name"]),
                    admin=bool(p.get("admin", False)),
                    created_at=float(p.get("created_at", 0.0)),
                    token_sha256=str(p["token_sha256"]),
                )
                for p in data.get("people", [])
            ]
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            if not self._broken:
                log.error(
                    "people file %s is unreadable (%s); personal tokens refused", self.path, e
                )
            self._sig, self._people, self._broken = sig, [], True
            return
        self._sig, self._people, self._broken = sig, people, False

    def list(self) -> list[Person]:
        with self._lock:
            self._refresh()
            return list(self._people)

    def get(self, name: str) -> Person | None:
        key = name.lower()
        return next((p for p in self.list() if p.name.lower() == key), None)

    def match(self, token: str) -> Person | None:
        """The person whose token this is. Compares against every hash (no early exit)."""
        if not token:
            return None
        given = token_hash(token)
        found = None
        for p in self.list():
            if hmac.compare_digest(given, p.token_sha256):
                found = p
        return found

    # -- writing ------------------------------------------------------------------------------
    def _save(self, people: list[Person]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".people.", suffix=".tmp")
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {
                        "version": 1,
                        "people": [
                            {
                                "name": p.name,
                                "admin": p.admin,
                                "created_at": p.created_at,
                                "token_sha256": p.token_sha256,
                            }
                            for p in people
                        ],
                    },
                    f,
                    indent=2,
                )
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        os.chmod(self.path, 0o600)
        self._sig = None  # re-read on next access

    def _edit(self) -> list[Person]:
        self._refresh()
        if self._broken:
            raise PeopleError(f"{self.path} is unreadable; fix or remove it first")
        return list(self._people)

    def _unique_token(self, people: list[Person]) -> tuple[str, str]:
        while True:
            token = new_token()
            h = token_hash(token)
            if all(p.token_sha256 != h for p in people):
                return token, h

    def add(self, name: str, admin: bool = False) -> tuple[Person, str]:
        check_person_name(name)
        with self._lock:
            people = self._edit()
            if any(p.name.lower() == name.lower() for p in people):
                raise PeopleError(f"{name!r} is already a person here")
            token, h = self._unique_token(people)
            person = Person(name, bool(admin), time.time(), h)
            self._save([*people, person])
        return person, token

    def _replace(self, name: str, **changes: Any) -> Person:
        people = self._edit()
        for i, p in enumerate(people):
            if p.name.lower() == name.lower():
                new = Person(
                    p.name,
                    changes.get("admin", p.admin),
                    p.created_at,
                    changes.get("token_sha256", p.token_sha256),
                )
                people[i] = new
                self._save(people)
                return new
        raise PeopleError(f"no person {name!r}")

    def reissue(self, name: str) -> tuple[Person, str]:
        with self._lock:
            token, h = self._unique_token(self._edit())
            return self._replace(name, token_sha256=h), token

    def set_admin(self, name: str, admin: bool) -> Person:
        with self._lock:
            return self._replace(name, admin=bool(admin))

    def revoke(self, name: str) -> Person:
        with self._lock:
            people = self._edit()
            gone = next((p for p in people if p.name.lower() == name.lower()), None)
            if gone is None:
                raise PeopleError(f"no person {name!r}")
            self._save([p for p in people if p is not gone])
            return gone
