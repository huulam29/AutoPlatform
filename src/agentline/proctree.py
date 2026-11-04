"""Kill a child process and everything it started, on POSIX and Windows.

POSIX children are started with `start_new_session=True`, so the process group is killed.
Windows has no process groups to signal: `taskkill /T /F` ends the process tree.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys


def kill_tree(pid: int) -> None:
    """Best effort: a process that already exited is not an error."""
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)  # TerminateProcess, in case taskkill is missing
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def resolve_argv(argv: list[str] | tuple[str, ...], env: dict[str, str] | None = None) -> list[str]:
    """On Windows, find a bare program name on PATH with its extension (`claude` ->
    `C:/.../claude.cmd`): CreateProcess only tries `.exe`, so npm/pip shims would not start.
    Elsewhere (or when not found) argv is returned unchanged."""
    out = list(argv)
    if sys.platform != "win32" or not out:
        return out
    prog = out[0]
    if os.sep in prog or "/" in prog or os.path.splitext(prog)[1]:
        return out
    path = env.get("PATH") if env is not None else None
    found = shutil.which(prog, path=path)
    if found:
        out[0] = found
    return out
