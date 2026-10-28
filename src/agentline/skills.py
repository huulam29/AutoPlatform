"""Agent skills: reusable instructions (a folder with a SKILL.md) that agent nodes load.

The library is fetched from GitHub, by default addyosmani/agent-skills (MIT), into
`$AGENTLINE_HOME/skills/<repo>/` and refreshed on demand (`agentline skills fetch`, Settings,
or the editor assistant). Nothing is fetched while a pipeline runs; a node that names a skill
the library doesn't have fails with a message saying so.

A node picks skills by name (`skills: [planning-and-task-breakdown, test-driven-development]`).
How they reach the agent:

- claude-code with a workspace: copied to `<workspace>/.claude/skills/<name>/`, where Claude
  Code discovers them itself.
- every other case: the SKILL.md bodies are appended to the prompt under "# Skills".

agentline also ships a few skills of its own (`builtin_skills/`, e.g. `interview-me` for the
Chat). Skills installed from other people's repos or the skills.sh marketplace
(`skill_sources.py`) sit in `<skills>/installed/`. On a name clash: your own > installed >
the fetched library > built-in.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError

DEFAULT_REPO = "addyosmani/agent-skills"
DEFAULT_REF = "main"
NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
MAX_SKILL_BYTES = 200_000

# What each kind of pipeline step should load (used by the editor assistant and the docs).
SUGGESTED: dict[str, list[str]] = {
    "spec": ["spec-driven-development", "idea-refine", "interview-me"],
    "plan": ["planning-and-task-breakdown", "spec-driven-development"],
    "design": ["api-and-interface-design", "documentation-and-adrs"],
    "implement": [
        "incremental-implementation",
        "test-driven-development",
        "source-driven-development",
    ],
    "frontend": ["frontend-ui-engineering", "browser-testing-with-devtools"],
    "test": ["test-driven-development", "browser-testing-with-devtools"],
    "review": ["code-review-and-quality", "security-and-hardening", "code-simplification"],
    "debug": ["debugging-and-error-recovery"],
    "security": ["security-and-hardening"],
    "performance": ["performance-optimization"],
    "docs": ["documentation-and-adrs"],
    "git": ["git-workflow-and-versioning"],
    "ci": ["ci-cd-and-automation"],
    "deploy": ["shipping-and-launch", "ci-cd-and-automation"],
    "observability": ["observability-and-instrumentation"],
    "migration": ["deprecation-and-migration"],
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # the skill's folder
    source: str = "fetched"  # fetched (the GitHub library) | installed | local (yours) | builtin

    @property
    def body(self) -> str:
        return (self.path / "SKILL.md").read_text(errors="replace")[:MAX_SKILL_BYTES]


BUILTIN_DIR = Path(__file__).parent / "builtin_skills"


def skills_root() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "skills"


def _frontmatter(text: str) -> dict[str, str]:
    """`name:` / `description:` from a SKILL.md's YAML front matter (simple `key: value`)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


class SkillLibrary:
    def __init__(self, root: str | Path | None = None, repo: str = DEFAULT_REPO) -> None:
        self.repo = repo
        self.base = Path(root) if root is not None else skills_root()
        self.dir = self.base / repo.replace("/", "__")

    @property
    def fetched(self) -> bool:
        return (self.dir / "skills").is_dir()

    def info(self) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        with contextlib.suppress(OSError, ValueError):
            meta = json.loads((self.dir / "source.json").read_text())
        return {
            "repo": self.repo,
            "dir": str(self.dir),
            "fetched": self.fetched,
            "fetched_at": meta.get("fetched_at"),
            "ref": meta.get("ref"),
        }

    @property
    def local_dir(self) -> Path:
        """Your own skills: `<skills>/local/<name>/SKILL.md` (they win over fetched ones)."""
        return self.base / "local"

    @property
    def installed_dir(self) -> Path:
        """Skills installed from a GitHub repo / skills.sh (agentline.skill_sources)."""
        return self.base / "installed"

    @staticmethod
    def _scan(root: Path, source: str) -> dict[str, Skill]:
        out: dict[str, Skill] = {}
        if not root.is_dir():
            return out
        for d in sorted(root.iterdir()):
            md = d / "SKILL.md"
            if not d.is_dir() or not md.is_file() or not NAME.match(d.name):
                continue
            fm = _frontmatter(md.read_text(errors="replace")[:8000])
            out[d.name] = Skill(d.name, fm.get("description", ""), d, source)
        return out

    def list(self) -> list[Skill]:
        merged = {
            **self._scan(BUILTIN_DIR, "builtin"),
            **self._scan(self.dir / "skills", "fetched"),
            **self._scan(self.installed_dir, "installed"),
            **self._scan(self.local_dir, "local"),
        }
        return [merged[k] for k in sorted(merged)]

    def save_local(self, name: str, description: str, body: str) -> Skill:
        """Create or replace your own skill. `body` is the markdown after the front matter."""
        if not NAME.match(name):
            raise ValidationError(f"bad skill name {name!r} (lowercase letters, digits, - _ .)")
        if len(body) > MAX_SKILL_BYTES:
            raise ValidationError("the skill is too long")
        d = self.local_dir / name
        d.mkdir(parents=True, exist_ok=True)
        desc = " ".join(description.split())
        text = f"---\nname: {name}\ndescription: {desc}\n---\n\n{body.strip()}\n"
        tmp = d / ".SKILL.md.tmp"
        tmp.write_text(text)
        os.replace(tmp, d / "SKILL.md")
        return Skill(name, desc, d, "local")

    def delete_local(self, name: str) -> bool:
        d = self.local_dir / name
        if not NAME.match(name) or not (d / "SKILL.md").is_file():
            return False
        shutil.rmtree(d)
        return True

    def get(self, name: str) -> Skill | None:
        if not NAME.match(name):
            return None
        return next((s for s in self.list() if s.name == name), None)

    def resolve(self, names: list[str] | tuple[str, ...]) -> list[Skill]:
        """The named skills, or a ValidationError naming the missing ones."""
        if not names:
            return []
        found = {s.name: s for s in self.list()}
        missing = [n for n in names if n not in found]
        if missing and not self.fetched:
            raise ValidationError(
                f"the node uses skills ({', '.join(missing)}) but the skill library "
                f"({self.repo}) hasn't been fetched: run `agentline skills fetch`"
            )
        if missing:
            raise ValidationError(
                f"unknown skill(s) {', '.join(missing)} (not in {self.repo}, your installed "
                "skills or your own skills)"
            )
        return [found[n] for n in names]

    # -- fetching ---------------------------------------------------------------------------
    def fetch(self, ref: str = DEFAULT_REF, timeout: float = 60) -> dict[str, Any]:
        """Download the repo's `skills/` folder (a GitHub tarball) and replace the local copy."""
        url = f"https://codeload.github.com/{self.repo}/tar.gz/{ref}"
        req = urllib.request.Request(url, headers={"user-agent": "agentline"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        return self.install_tarball(data, ref)

    def install_tarball(self, data: bytes, ref: str = DEFAULT_REF) -> dict[str, Any]:
        """Unpack only `<top>/skills/**` and `<top>/LICENSE` (regular files, no links, nothing
        outside the folder), then swap it in atomically."""
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=self.base, prefix=".fetch-"))
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                for m in tar.getmembers():
                    parts = Path(m.name).parts
                    if len(parts) < 2 or not m.isfile() or ".." in parts:
                        continue
                    rel = Path(*parts[1:])
                    if rel.parts[0] != "skills" and rel != Path("LICENSE"):
                        continue
                    if m.size > 5_000_000:
                        continue
                    dest = tmp / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    src = tar.extractfile(m)
                    if src is not None:
                        dest.write_bytes(src.read())
            if not (tmp / "skills").is_dir():
                raise ValidationError(f"{self.repo}@{ref} has no skills/ folder")
            (tmp / "source.json").write_text(
                json.dumps({"repo": self.repo, "ref": ref, "fetched_at": time.time()})
            )
            old = self.dir.with_name(self.dir.name + ".old")
            if old.exists():
                shutil.rmtree(old)
            if self.dir.exists():
                os.replace(self.dir, old)
            os.replace(tmp, self.dir)
            if old.exists():
                shutil.rmtree(old)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        return {**self.info(), "count": len(self.list())}


def install_into(workspace_path: Path, skills: list[Skill]) -> list[str]:
    """Copy skills into `<workspace>/.claude/skills/` (where Claude Code finds them), and keep
    them out of git (the checkout's info/exclude) so an agent's commit doesn't pick them up."""
    dest_root = workspace_path / ".claude" / "skills"
    out = []
    for s in skills:
        dest = dest_root / s.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(s.path, dest, symlinks=False)
        out.append(s.name)
    _git_exclude(workspace_path, "/.claude/skills/")
    return out


def _git_exclude(path: Path, pattern: str) -> None:
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True, text=True, timeout=10, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return
    if r.returncode != 0 or not r.stdout.strip():
        return
    exclude = Path(r.stdout.strip())
    if not exclude.is_absolute():
        exclude = path / exclude
    try:
        text = exclude.read_text() if exclude.exists() else ""
        if pattern not in text.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(
                text + ("" if not text or text.endswith("\n") else "\n") + pattern + "\n"
            )
    except OSError:
        pass


def prompt_section(skills: list[Skill]) -> str:
    """The skills as prompt text, for agents that can't load skill folders."""
    if not skills:
        return ""
    parts = ["\n\n# Skills\n\nFollow these skills while you work.\n"]
    for s in skills:
        parts.append(f"\n## Skill: {s.name}\n\n{s.body.strip()}\n")
    return "".join(parts)
