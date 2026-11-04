"""`agentline serve` exits by itself when the process that started it (the Mac app) dies."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("uvicorn", reason="needs the server extra")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.slow
@pytest.mark.skipif(sys.platform == "win32", reason="the parent watchdog is POSIX-only")
def test_engine_stops_when_parent_dies(tmp_path: Path) -> None:
    (tmp_path / "pipelines").mkdir()
    port = free_port()
    # A stand-in "app": a shell that starts the engine with its own pid, then waits.
    code = (
        "import os, subprocess, sys, time\n"
        "env = dict(os.environ, AGENTLINE_PARENT_PID=str(os.getpid()))\n"
        f"db, pipes = {str(tmp_path / 'a.db')!r}, {str(tmp_path / 'pipelines')!r}\n"
        "argv = [sys.executable, '-m', 'agentline.cli', '--db', db, '--pipelines', pipes]\n"
        f"p = subprocess.Popen(argv + ['serve', '--port', '{port}'], env=env)\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(600)\n"
    )
    env = {
        **os.environ,
        "AGENTLINE_HOME": str(tmp_path / "home"),
        "AGENTLINE_CONFIG": str(tmp_path / "none.toml"),
    }
    parent = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env
    )
    assert parent.stdout is not None
    engine_pid = int(parent.stdout.readline())
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:  # wait until it serves
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    parent.send_signal(signal.SIGKILL)  # the app crashes
    parent.wait()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(engine_pid, 0)
        except ProcessLookupError:
            return  # the engine noticed and stopped
        time.sleep(0.2)
    os.kill(engine_pid, signal.SIGKILL)
    pytest.fail("the engine kept running after its parent died")
