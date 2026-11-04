"""A blocking, exclusive, cross-process lock on a lock file; works on POSIX and Windows.

    with locked_file(root / ".lock"):
        ...  # read-modify-write files that other processes also write

POSIX uses `fcntl.flock`, Windows `msvcrt.locking` on the file's first byte (retried, since
Windows has no blocking lock without a 10-second give-up). The OS drops the lock when the
process exits, so a leftover lock file never blocks anyone.
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Iterator
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

    def _lock(f) -> None:  # type: ignore[no-untyped-def]
        while True:
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:  # held by another handle
                time.sleep(0.01)

    def _unlock(f) -> None:  # type: ignore[no-untyped-def]
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(f) -> None:  # type: ignore[no-untyped-def]
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f) -> None:  # type: ignore[no-untyped-def]
        fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def locked_file(path: str | Path) -> Iterator[None]:
    """Hold an exclusive lock on `path` (created if missing) for the `with` block."""
    with open(path, "a+", encoding="utf-8") as f:
        _lock(f)
        try:
            yield
        finally:
            _unlock(f)
