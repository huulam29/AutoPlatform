"""GitHub for failure-triage reports (Settings > General > GitHub; agentline.triage uses it).

Sign-in: the GitHub CLI's browser login (`gh auth login --web`), started from the app. It works
with SSO organisations: the app shows gh's one-time code and the device URL, the user approves in
the browser, and agentline then uses `gh auth token`. A pasted token (`GITHUB_TOKEN`, in the
environment or the secrets file) wins over the CLI login. Tokens are never returned by the API.

Repo: `[report] github_repo` in agentline.toml. SSH or HTTPS git remotes of agentline's own
checkout and of the pipelines folder are detected, so the user picks one instead of typing it
(an SSH key itself can't open issues: the GitHub API takes tokens only).
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError

HOST = "github.com"
GH_CANDIDATES = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh")
TOKEN_TTL = 30.0
_REMOTE = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com(?::\d+)?/|https://(?:[^@/]+@)?github\.com/)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_CODE = re.compile(r"one-time code[^A-Z0-9]*([A-Z0-9]{4}-[A-Z0-9]{4})", re.I)
_URL = re.compile(r"https://github\.com/login/device\S*")
_cache: dict[str, tuple[float, str]] = {}


# -- the GitHub CLI ---------------------------------------------------------------------------
def gh_path() -> str | None:
    """`gh` on PATH, or where Homebrew puts it (the Mac app's PATH doesn't have Homebrew)."""
    found = shutil.which("gh")
    if found:
        return found
    return next((p for p in GH_CANDIDATES if os.access(p, os.X_OK)), None)


def _gh(*args: str, timeout: float = 10.0) -> tuple[int, str, str]:
    gh = gh_path()
    if gh is None:
        return 127, "", "gh isn't installed"
    try:
        p = subprocess.run(
            [gh, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return p.returncode, p.stdout, p.stderr


def gh_token() -> str:
    """The GitHub CLI login's token ("" when not signed in); cached for a few seconds."""
    hit = _cache.get("gh")
    if hit and time.monotonic() - hit[0] < TOKEN_TTL:
        return hit[1]
    code, out, _ = _gh("auth", "token", "--hostname", HOST)
    token = out.strip() if code == 0 else ""
    _cache["gh"] = (time.monotonic(), token)
    return token


def gh_user() -> str | None:
    """The signed-in account, from `gh auth status` (no network call of ours)."""
    code, out, err = _gh("auth", "status", "--hostname", HOST)
    m = re.search(r"account (\S+)", out + err) or re.search(r"as (\S+)", out + err)
    return m.group(1).strip("()") if code == 0 and m else None


def token() -> tuple[str, str | None]:
    """(token, where from: env | secrets | gh | None). A pasted token wins over the CLI."""
    from agentline.secrets import read_secrets

    stored = read_secrets().get("GITHUB_TOKEN", "")
    env = os.environ.get("GITHUB_TOKEN", "")
    if env and env != stored:  # (a saved key is put into the environment too)
        return env, "env"
    if stored:
        return stored, "secrets"
    t = gh_token()
    return (t, "gh") if t else ("", None)


# -- the browser sign-in ----------------------------------------------------------------------
class Login:
    """One `gh auth login --web` at a time: its code, URL and outcome."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen[str] | None = None
        self.state = "idle"  # idle | waiting | done | failed | cancelled
        self.code: str | None = None
        self.url: str | None = None
        self.error: str | None = None

    def view(self) -> dict[str, Any]:
        return {"state": self.state, "code": self.code, "url": self.url, "error": self.error}

    def start(self, wait: float = 15.0) -> dict[str, Any]:
        gh = gh_path()
        if gh is None:
            raise ValidationError(
                "the GitHub CLI isn't installed: `brew install gh`, or paste a token instead"
            )
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return self.view()
            self.state, self.code, self.url, self.error = "waiting", None, None, None
            env = {**os.environ, "GH_BROWSER": "true", "BROWSER": "true", "GH_PROMPT_DISABLED": ""}
            self.proc = subprocess.Popen(
                [gh, "auth", "login", "--hostname", HOST, "--git-protocol", "https", "--web",
                 "--skip-ssh-key"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=env,
            )  # fmt: skip
            proc = self.proc
        with contextlib.suppress(OSError):
            assert proc.stdin is not None
            proc.stdin.write("\n")  # "press Enter to open the browser"
            proc.stdin.flush()
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and self.state == "waiting" and not self.code:
            time.sleep(0.05)
        return self.view()

    def _watch(self, proc: subprocess.Popen[str]) -> None:
        tail: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            tail = [*tail, line.strip()][-5:]
            if (m := _CODE.search(line)) and not self.code:
                self.code = m.group(1)
            if (u := _URL.search(line)) and not self.url:
                self.url = u.group(0)
        code = proc.wait()
        with self.lock:
            if self.proc is not proc or self.state == "cancelled":
                return
            _cache.pop("gh", None)
            if code == 0:
                self.state = "done"
            else:
                self.state = "failed"
                self.error = next((t for t in reversed(tail) if t), f"gh exited with {code}")

    def cancel(self) -> dict[str, Any]:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.kill()
            self.state = "cancelled"
        return self.view()


login = Login()


# -- the repo ---------------------------------------------------------------------------------
def parse_remote(url: str) -> tuple[str, str] | None:
    """('owner/name', 'ssh' | 'https') for a github.com remote URL."""
    m = _REMOTE.match(url.strip())
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}", "https" if url.startswith("https") else "ssh"


def source_dirs(files: Any = None) -> list[tuple[str, Path]]:
    """Folders whose git remotes may be agentline's repo: its own checkout, the pipelines."""
    out = [("agentline", Path(__file__).resolve().parents[2])]
    if files is not None and getattr(files, "root", None):
        out.append(("pipelines folder", Path(files.root)))
    return out


def detect_repos(dirs: list[tuple[str, Path]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for label, d in dirs:
        if not d.is_dir():
            continue
        try:
            p = subprocess.run(
                ["git", "-C", str(d), "remote", "-v"],
                capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in p.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and (hit := parse_remote(parts[1])):
                repo, proto = hit
                seen.setdefault(repo, {"repo": repo, "remote": parts[0], "protocol": proto,
                                       "where": label, "url": parts[1]})  # fmt: skip
    return list(seen.values())


# -- status, save, test -----------------------------------------------------------------------
def status(cfg: Any, files: Any = None) -> dict[str, Any]:
    from agentline.triage import report_config

    rc = report_config(cfg)
    tok, source = token()
    gh = gh_path()
    logged_in = bool(gh_token()) if gh else False
    return {
        "repo": rc["github_repo"] or None,
        "auto": rc["github_auto"],
        "labels": rc["labels"],
        "error": rc["error"],
        "token_source": source,
        "ready": bool(rc["github_repo"]) and bool(tok),
        "gh": {
            "installed": bool(gh),
            "logged_in": logged_in,
            "user": gh_user() if logged_in else None,
        },
        "detected": detect_repos(source_dirs(files)),
        "login": login.view(),
    }


def save(path: Path, repo: str | None, auto: bool | None = None) -> None:
    """Write `[report]` (github_repo, github_auto) to agentline.toml, keeping other keys."""
    import tomllib

    from agentline.config import Config
    from agentline.integrations.github import split_repo
    from agentline.tomlwrite import set_plain_table, write_file

    repo = (repo or "").strip()
    if repo:
        hit = parse_remote(repo)
        repo = hit[0] if hit else repo
        try:
            split_repo(repo)
        except ValueError as e:
            raise ValidationError(str(e)) from None
    text = path.read_text() if path.exists() else ""
    current = dict(tomllib.loads(text).get("report") or {}) if text.strip() else {}
    current["github_repo"] = repo or None
    if auto is not None:
        current["github_auto"] = bool(auto) or None
    new = set_plain_table(text, "report", current)
    Config.from_dict(tomllib.loads(new), path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file(path, new)


def test(cfg: Any) -> dict[str, Any]:
    """Can agentline open issues in the repo with the token it has?"""
    from agentline.integrations.github import GitHubClient
    from agentline.integrations.http import ApiError
    from agentline.triage import report_config

    repo = report_config(cfg)["github_repo"]
    tok, source = token()
    if not repo:
        return {"ok": False, "error": "choose the repo first"}
    if not tok:
        return {"ok": False, "error": "sign in with GitHub or paste a token first"}
    client = GitHubClient(tok, os.environ.get("GITHUB_API_URL") or "")
    try:
        data = client.get_repo(repo)
    except ApiError as e:
        hint = {
            401: "GitHub refused the token (401): sign in again or paste a new token",
            403: "the token can't read that repo (403): give it access to the repo",
            404: f"{repo} wasn't found, or the token can't see it (404)",
        }.get(e.status or 0, str(e))
        return {"ok": False, "error": hint, "token_source": source}
    issues_on = bool(data.get("has_issues", True))
    return {
        "ok": issues_on,
        "repo": data.get("full_name") or repo,
        "private": bool(data.get("private")),
        "token_source": source,
        "error": None if issues_on else "issues are turned off in that repo",
    }
