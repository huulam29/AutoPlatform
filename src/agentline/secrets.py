"""Local secrets file: `$AGENTLINE_HOME/secrets.env` (default `~/.agentline/secrets.env`).

API keys entered in the GUI land here, never in agentline.toml, the ledger or logs. The file
holds `NAME=value` lines, has owner-only permissions (0600) and is written atomically. It is
loaded into the process environment at startup without overriding variables already set, so a
key from your shell wins. The API can set or delete a key and report whether one is set, but
never returns a value.
"""

from __future__ import annotations

import os
import re
import tempfile
import weakref
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError

NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def secrets_path() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "secrets.env"


def check_name(name: str) -> str:
    if not NAME.match(name):
        raise ValidationError(f"{name!r} is not a valid env var name (A-Z, 0-9, _)")
    return name


def read_secrets(path: Path | None = None) -> dict[str, str]:
    p = path or secrets_path()
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if NAME.match(k.strip()):
            out[k.strip()] = v
    return out


def _write(values: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".secrets.", suffix=".tmp")
    try:
        if hasattr(os, "fchmod"):  # POSIX; on Windows the per-user profile ACL protects it
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# agentline secrets: written by the GUI/CLI; keep this file private\n")
            for k in sorted(values):
                f.write(f"{k}={values[k]}\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def set_secret(name: str, value: str, path: Path | None = None) -> None:
    """Store a secret and put it in this process's environment."""
    check_name(name)
    if "\n" in value or "\r" in value:
        raise ValidationError("secret values can't contain newlines")
    if not value:
        raise ValidationError("secret value is empty")
    p = path or secrets_path()
    values = read_secrets(p)
    values[name] = value
    _write(values, p)
    os.environ[name] = value
    _register_with_redactors(name, value)


def delete_secret(name: str, path: Path | None = None) -> bool:
    check_name(name)
    p = path or secrets_path()
    values = read_secrets(p)
    if name not in values:
        return False
    old = values.pop(name)
    _write(values, p)
    if os.environ.get(name) == old:
        os.environ.pop(name)
    return True


def load_secrets(path: Path | None = None) -> list[str]:
    """Put the file's secrets into os.environ (existing variables win). Returns the names."""
    values = read_secrets(path)
    for k, v in values.items():
        os.environ.setdefault(k, v)
        _register_with_redactors(k, v)
    return sorted(values)


def sync_secrets(before: dict[str, str], path: Path | None = None) -> dict[str, str]:
    """The secrets file changed outside this process (the Mac app's Keys screen, a hand edit):
    bring os.environ along. A variable the file set before follows the file; one set some
    other way (the shell) still wins. `before` is the file as last read; returns it now."""
    now = read_secrets(path)
    for k, v in now.items():
        if os.environ.get(k) in (None, before.get(k)):
            os.environ[k] = v
        _register_with_redactors(k, v)
    for k in set(before) - set(now):
        if os.environ.get(k) == before[k]:
            os.environ.pop(k)
    return now


def secret_status(names: list[str], path: Path | None = None) -> list[dict[str, object]]:
    """For each name: is it set, and where from (env or the secrets file). Never the value."""
    stored = read_secrets(path)
    out = []
    for n in sorted(set(names)):
        if n in os.environ and stored.get(n) != os.environ[n]:
            source = "environment"
        elif n in stored:
            source = "secrets file"
        else:
            source = None
        out.append({"name": n, "set": source is not None, "source": source})
    return out


_REDACTORS: weakref.WeakSet[Any] = weakref.WeakSet()


def track_redactor(redactor: Any) -> None:
    """Redactors created before a secret is added from the GUI learn it too."""
    _REDACTORS.add(redactor)


def _register_with_redactors(name: str, value: str) -> None:
    for r in list(_REDACTORS):
        add = getattr(r, "add", None)
        if callable(add):
            add(name, value)
