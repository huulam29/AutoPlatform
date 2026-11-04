"""Pipeline history: every change to the pipelines folder is a commit, so any version can be
looked at again and restored.

A private git repository: `--git-dir <pipelines>/.agentline-history --work-tree <pipelines>`.
It never touches a repository the folder may already be in (and the editor skips dot
folders). Saves from the editor, the assistant's Apply, imports and restores each record a
commit. Without `git` on PATH history is simply off (`available()` is False).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

GIT_DIR = ".agentline-history"
_lock = threading.Lock()


def available() -> bool:
    return shutil.which("git") is not None


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "agentline",
        "GIT_AUTHOR_EMAIL": "agentline@localhost",
        "GIT_COMMITTER_NAME": "agentline",
        "GIT_COMMITTER_EMAIL": "agentline@localhost",
    }
    return subprocess.run(
        ["git", f"--git-dir={root / GIT_DIR}", f"--work-tree={root}", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=check,
    )


def _ensure(root: Path) -> None:
    if (root / GIT_DIR / "HEAD").exists():
        return
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", "--bare", str(root / GIT_DIR)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )  # fmt: skip
    _git(root, "config", "core.bare", "false")
    exclude = root / GIT_DIR / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(
        f"/{GIT_DIR}/\n.DS_Store\n__pycache__/\nnode_modules/\n.*.tmp\n", encoding="utf-8"
    )


def record(root: str | Path, message: str) -> str | None:
    """Commit the folder's current state if anything changed. Returns the commit id (None
    when nothing changed or git isn't there). Never raises: history must not break a save."""
    if not available():
        return None
    root = Path(root).resolve()
    with _lock:
        try:
            _ensure(root)
            _git(root, "add", "-A", ".")
            if not _git(root, "status", "--porcelain", check=False).stdout.strip():
                return None
            _git(root, "commit", "--quiet", "--no-verify", "-m", message[:500])
            return _git(root, "rev-parse", "HEAD").stdout.strip()
        except (subprocess.SubprocessError, OSError):
            return None


def log(root: str | Path, path: str, limit: int = 100) -> list[dict[str, Any]]:
    """Commits that touched `path` (relative to the folder), newest first."""
    root = Path(root).resolve()
    if not available() or not (root / GIT_DIR / "HEAD").exists():
        return []
    r = _git(
        root, "log", f"-n{int(limit)}", "--format=%H%x1f%ct%x1f%s", "--follow", "--", path,
        check=False,
    )  # fmt: skip
    out = []
    for line in r.stdout.splitlines():
        sha, at, subject = [*line.split("\x1f"), "", ""][:3]
        if sha:
            out.append({"commit": sha, "at": float(at or 0), "message": subject})
    return out


def show(root: str | Path, commit: str, path: str) -> str | None:
    """`path`'s content at `commit` (None if it didn't exist there)."""
    root = Path(root).resolve()
    if not all(c in "0123456789abcdef" for c in commit) or not 4 <= len(commit) <= 64:
        return None
    if not available() or not (root / GIT_DIR / "HEAD").exists():
        return None
    r = _git(root, "show", f"{commit}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None
