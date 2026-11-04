"""Fake command-line programs for tests, on POSIX and Windows.

    fake_bin(tmp_path, "claude", script=FAKE)       # runs `python FAKE "$@"`
    fake_bin(tmp_path, "claude", code="print(1)")   # writes the Python next to the wrapper
    fake_bin(tmp_path, "claude")                    # exists on PATH, does nothing

POSIX gets an executable `#!/bin/sh` wrapper named `name`; Windows gets `name.cmd` (found by
`shutil.which` through PATHEXT and started by the engine's `resolve_argv`). The returned path
is the wrapper; pass it as `bin` or put its folder on PATH.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

WINDOWS = sys.platform == "win32"


def fake_bin(
    folder: Path, name: str, *, script: str | Path | None = None, code: str | None = None
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if code is not None:
        script = folder / f"_{name}_fake.py"
        Path(script).write_text(code)
    if WINDOWS:
        wrapper = folder / f"{name}.cmd"
        body = f'@"{sys.executable}" "{script}" %*\r\n' if script else "@exit /b 0\r\n"
        wrapper.write_text("@echo off\r\n" + body, newline="")
        return wrapper
    wrapper = folder / name
    body = f'exec "{sys.executable}" "{script}" "$@"\n' if script else ""
    wrapper.write_text("#!/bin/sh\n" + body)
    wrapper.chmod(0o755)
    return wrapper


# `shell.run` / `env.*` string commands go through /bin/sh; on Windows they would go through
# cmd.exe, which the tests' (and users') shell syntax doesn't speak.
posix_shell = pytest.mark.skipif(WINDOWS, reason="shell commands use POSIX sh syntax")
