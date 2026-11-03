"""The secret checks in .githooks/pre-commit, run on a throwaway repository.

Staged files here are .txt or credential files, so the hook never reaches `uv run ruff`.
"""

import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).parents[1] / ".githooks" / "pre-commit"
KEY = "sk-ant-api03-" + "abcDEF0123456789" * 2


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def stage(repo: Path, path: str, text: str) -> subprocess.CompletedProcess[str]:
    git(repo, "init", "-q") if not (repo / ".git").exists() else None
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(repo, "add", "--force", "--", path)
    return subprocess.run(["sh", str(HOOK)], cwd=repo, capture_output=True, text=True)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "src/.env.local",
        "secrets.env",
        "deploy/server.pem",
        "tls.key",
        "id_rsa",
        "tests/.env",  # credential files are refused everywhere, tests included
    ],
)
def test_refuses_credential_files(tmp_path: Path, path: str) -> None:
    done = stage(tmp_path, path, "harmless\n")
    assert done.returncode == 1
    assert path in done.stderr


@pytest.mark.parametrize(
    "line",
    [
        KEY,
        "ghp_" + "a1B2" * 10,
        "xoxb-1234567890-abcdef",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "-----BEGIN RSA PRIVATE KEY-----",
        "https://hooks.slack.com/services/T000/B000/xxx",
    ],
)
def test_refuses_key_shaped_lines(tmp_path: Path, line: str) -> None:
    done = stage(tmp_path, "notes.txt", f"before\nvalue = {line}\n")
    assert done.returncode == 1
    assert "notes.txt" in done.stderr
    assert line not in done.stderr + done.stdout


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("notes.txt", "task-with-a-very-long-kebab-case-name-here\n"),  # "sk-" inside a word
        ("notes.txt", "ANTHROPIC_API_KEY=<your key>\n"),
        (".env.example", "ANTHROPIC_API_KEY=\n"),
        ("tests/fixture.txt", f"{KEY}\n"),  # made-up keys are fine in tests
        ("macos/Tests/fixture.txt", f"{KEY}\n"),
    ],
)
def test_allows(tmp_path: Path, path: str, text: str) -> None:
    done = stage(tmp_path, path, text)
    assert done.returncode == 0, done.stderr


def test_only_added_lines_count(tmp_path: Path) -> None:
    stage(tmp_path, "notes.txt", f"{KEY}\n")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qnm", "old")
    done = stage(tmp_path, "notes.txt", f"{KEY}\nnew line\n")
    assert done.returncode == 0, done.stderr


def test_hook_comes_with_a_gitignore_for_the_same_files() -> None:
    ignored = (HOOK.parents[1] / ".gitignore").read_text().splitlines()
    for name in (".env", ".env.*", "secrets.env", "*.pem", "*.key", "id_rsa"):
        assert name in ignored
