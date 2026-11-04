"""Show a path in the system file manager, for the web UI's "Show in folder" (spec 006 FR-011).

Only paths inside one of the allowed roots (data folder, pipelines folder, the settings file's
folder) are opened, with no shell: `open -R` on macOS, `explorer /select,` on Windows and
`xdg-open <folder>` elsewhere. The API route also refuses requests that aren't local.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


def allowed(path: str | Path, roots: Iterable[str | Path]) -> bool:
    """True when `path` (symlinks resolved) is one of `roots` or inside one."""
    p = Path(path).expanduser().resolve()
    for root in roots:
        r = Path(root).expanduser().resolve()
        if p == r or p.is_relative_to(r):
            return True
    return False


def command(path: str | Path, platform: str | None = None) -> list[str]:
    p = Path(path)
    plat = platform or sys.platform
    if plat == "darwin":
        return ["open", "-R", str(p)]
    if plat == "win32":
        return ["explorer", f"/select,{p}"]
    return ["xdg-open", str(p if p.is_dir() else p.parent)]


def reveal(path: str | Path) -> None:
    subprocess.Popen(command(path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
