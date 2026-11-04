"""One engine per data folder: `agentline serve` holds an exclusive lock on `<home>/engine.lock`
for its lifetime (spec 006 FR-028).

    lock = EngineLock(home)
    lock.acquire("http://127.0.0.1:8080", wait=0.0)   # EngineLocked(pid, address) when held
    ...
    lock.release()

The OS releases the lock when the process exits (crash or kill included), so a leftover file
never blocks; its content (`{"pid", "address"}`) only names the holder in the error. POSIX uses
`fcntl.flock`, Windows `msvcrt.locking`. A filesystem without locking logs a warning and goes
on unlocked. `wait` retries for that many seconds (the Mac app's engine restart can start the
new engine while the old one is still exiting).
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import IO, Any

log = logging.getLogger("agentline.enginelock")
LOCK_FILE = "engine.lock"
# Windows locks are mandatory: lock one byte far past the content, so another process can still
# read who holds the lock.
_WIN_LOCK_OFFSET = 1 << 30


class EngineLocked(RuntimeError):
    def __init__(self, pid: int | None, address: str | None) -> None:
        self.pid, self.address = pid, address
        super().__init__(
            f"another engine is running on this data folder ({address or 'unknown address'},"
            f" pid {pid if pid is not None else 'unknown'})"
        )


def _try_lock(f: IO[str]) -> bool:
    """True when locked; False when another process holds it. Other errors propagate."""
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(f.fileno(), _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK):
            return False
        raise
    return True


def _holder(path: Path) -> dict[str, Any]:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        if isinstance(data, dict):
            return data
    return {}


class EngineLock:
    def __init__(self, home: str | Path) -> None:
        self.path = Path(home) / LOCK_FILE
        self._file: IO[str] | None = None

    def acquire(self, address: str, wait: float = 0.0) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 (held open for the process's lifetime)
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                locked = _try_lock(f)
            except OSError as e:
                log.warning("data folder lock not supported here (%s); starting unlocked", e)
                f.close()
                return
            if locked:
                break
            if time.monotonic() >= deadline:
                f.close()
                held = _holder(self.path)
                raise EngineLocked(held.get("pid"), held.get("address"))
            time.sleep(0.2)
        # The holder writes its own details (another handle couldn't on Windows).
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"pid": os.getpid(), "address": address}))
        f.flush()
        self._file = f

    def release(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()  # closing releases the lock
            self._file = None
