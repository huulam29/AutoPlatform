"""Temp git "remotes", a minimal StepContext stand-in, and a fake docker executor."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentline.runners.common import GIT_IDENTITY


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_IDENTITY}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_remote(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for name, text in (files or {"README.md": "hello\n"}).items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


@dataclass
class FakeExecution:
    id: str
    run_id: str
    node_id: str = "implement"
    iteration: int = 1
    attempt: int = 1


@dataclass
class FakeCtx:
    execution: FakeExecution
    payload: Any = None
    notes: dict[str, Any] = field(default_factory=dict)
    attachments: dict[str, str] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        return self.execution.run_id

    @property
    def node_id(self) -> str:
        return self.execution.node_id

    @property
    def iteration(self) -> int:
        return self.execution.iteration

    async def attach(self, name: str, data: str | bytes) -> str:
        self.attachments[name] = data if isinstance(data, str) else data.decode()
        return f"ref-{name}"


def ctx(run_id: str = "run1", exec_id: str = "ex1", iteration: int = 1, payload: Any = None) -> Any:
    return FakeCtx(FakeExecution(exec_id, run_id, iteration=iteration), payload)


class FakeDocker:
    """Records every docker argv; answers like a docker CLI with no images."""

    def __init__(self, have_images: set[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.images = set(have_images or ())
        self.exec_reply: tuple[int, str, str] = (0, '{"ok": true}', "")

    async def __call__(
        self,
        argv: list[str],
        *,
        cwd: Any = None,
        env: dict[str, str] | None = None,
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        self.calls.append({"argv": argv, "env": dict(env or {}), "input": input})
        sub = argv[1:3]
        if sub == ["image", "inspect"]:
            return (0, "[]", "") if argv[3] in self.images else (1, "", "No such image")
        if argv[1] == "build":
            self.images.add(argv[argv.index("-t") + 1])
            return 0, "", ""
        if argv[1] == "exec":
            return self.exec_reply
        if argv[1] == "run":
            return 0, "c0ffee\n", ""
        return 0, "", ""

    def argvs(self, sub: str) -> list[list[str]]:
        return [c["argv"] for c in self.calls if c["argv"][1] == sub]
