"""`rmtree` that also removes read-only files (git's object files are read-only, and Windows
refuses to delete a read-only file; POSIX only checks the folder's permissions)."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _retry_writable(func: Callable[..., Any], path: str, _exc: Any) -> None:
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)


def rmtree(path: str | Path, ignore_errors: bool = False) -> None:
    def clear(func: Callable[..., Any], p: str, exc: Any) -> None:
        try:
            _retry_writable(func, p, exc)
        except OSError:
            if not ignore_errors:
                raise

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear)
    else:
        shutil.rmtree(path, onerror=clear)
