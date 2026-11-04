"""One engine per data folder (agentline.enginelock)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agentline.enginelock import EngineLock, EngineLocked

HOLD = textwrap.dedent(
    """
    import sys, time
    from agentline.enginelock import EngineLock
    lock = EngineLock(sys.argv[1]); lock.acquire("http://127.0.0.1:9", wait=0)
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
    """
)


def hold(home: Path, seconds: float) -> subprocess.Popen[str]:
    p = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(home), str(seconds)], stdout=subprocess.PIPE, text=True
    )
    assert p.stdout is not None and p.stdout.readline().strip() == "held"
    return p


def test_a_second_process_is_refused_and_named(tmp_path: Path) -> None:
    p = hold(tmp_path, 30)
    try:
        with pytest.raises(EngineLocked) as e:
            EngineLock(tmp_path).acquire("http://127.0.0.1:1")
        assert e.value.pid == p.pid and e.value.address == "http://127.0.0.1:9"
        assert f"pid {p.pid}" in str(e.value)
    finally:
        p.kill()
        p.wait()


def test_a_killed_holder_never_blocks(tmp_path: Path) -> None:
    p = hold(tmp_path, 30)
    p.kill()
    p.wait()
    lock = EngineLock(tmp_path)
    lock.acquire("http://127.0.0.1:2")  # the file is still there; the lock went with the process
    lock.release()


def test_wait_takes_the_lock_once_the_holder_exits(tmp_path: Path) -> None:
    p = hold(tmp_path, 0.6)
    start = time.monotonic()
    lock = EngineLock(tmp_path)
    lock.acquire("http://127.0.0.1:3", wait=10)
    assert time.monotonic() - start < 8
    lock.release()
    p.wait()


def test_different_data_folders_dont_clash(tmp_path: Path) -> None:
    a, b = EngineLock(tmp_path / "a"), EngineLock(tmp_path / "b")
    a.acquire("x")
    b.acquire("y")
    a.release()
    b.release()
