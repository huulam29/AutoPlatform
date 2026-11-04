"""Container image resolution, hash-tagged builds, and the deterministic image proposer
(ARCHITECTURE §11.1).

Resolution order (first match wins):
1. the node's `workspace.image` when it isn't `auto`;
2. the repo's `image` or `image_profile` in agentline.toml;
3. `auto`: `.devcontainer/devcontainer.json` `image` -> `Dockerfile.agentline` -> marker
   files (pyproject.toml / requirements*.txt = python, package.json = node, go.mod = go;
   several -> a combined profile such as `node+python`) -> the base image.

Profiles come from `[images.<profile>]` (`from`, `apt`/`packages`, `run`) with small
built-in defaults. A built image is tagged `agentline/img-<repo>-<sha256(inputs)[:12]>`, and
`build_image()` checks `docker image inspect` first, so building is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentline.config import Config, RepoConfig
from agentline.ids import canonical_json
from agentline.runners.common import Executor, base_env, run_process

DEFAULT_BASE = "agentline/runner-base:latest"
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "python": {"apt": ["python3", "python3-venv", "python3-pip"]},
    "node": {"apt": ["nodejs", "npm"]},
    "go": {"apt": ["golang-go"]},
}
MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "python"),
    ("requirements*.txt", "python"),
    ("setup.py", "python"),
    ("package.json", "node"),
    ("go.mod", "go"),
)


@dataclass
class ImageChoice:
    ref: str  # what `docker run` uses
    source: str  # explicit | repo | devcontainer | dockerfile | profile | base
    profile: str | None = None
    markers: list[str] = field(default_factory=list)
    dockerfile: str | None = None  # set when the image must be built
    context: Path | None = None  # build context (None = empty dir)

    @property
    def needs_build(self) -> bool:
        return self.dockerfile is not None

    def note(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "source": self.source,
            "profile": self.profile,
            "markers": self.markers,
            "built": self.needs_build,
        }


def detect_markers(worktree: Path) -> list[str]:
    found: list[str] = []
    for pattern, _ in MARKERS:
        found += sorted(p.name for p in worktree.glob(pattern) if p.is_file())
    return found


def profile_for_markers(markers: list[str]) -> str | None:
    langs = sorted({lang for pattern, lang in MARKERS for m in markers if Path(m).match(pattern)})
    return "+".join(langs) or None


def _strip_jsonc(text: str) -> str:
    """devcontainer.json allows comments and trailing commas; drop the common forms."""
    out = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = re.sub(r"^\s*//.*$", "", out, flags=re.M)
    return re.sub(r",(\s*[}\]])", r"\1", out)


def devcontainer_image(worktree: Path) -> str | None:
    for rel in (".devcontainer/devcontainer.json", ".devcontainer.json"):
        p = worktree / rel
        if p.is_file():
            text = p.read_text(errors="replace", encoding="utf-8")
            for candidate in (text, _strip_jsonc(text)):
                try:
                    data = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                image = data.get("image") if isinstance(data, dict) else None
                return image if isinstance(image, str) and image else None
    return None


def image_tag(repo: str, inputs: Any) -> str:
    digest = hashlib.sha256(canonical_json(inputs).encode()).hexdigest()[:12]
    safe = re.sub(r"[^a-z0-9_.-]+", "-", repo.lower()).strip("-") or "repo"
    return f"agentline/img-{safe}-{digest}"


def _profile_layers(name: str, images: dict[str, Any], seen: frozenset[str]) -> list[Any]:
    if name in seen:
        return []
    if name in images:
        d = images[name] or {}
        out: list[Any] = []
        for ext in d.get("extends") or ():
            out += _profile_layers(str(ext), images, seen | {name})
        return [*out, d]
    if "+" in name:
        return [x for part in name.split("+") for x in _profile_layers(part, images, seen)]
    return [BUILTIN_PROFILES.get(name, {})]


def profile_definition(profile: str, config: Config | None) -> dict[str, Any]:
    """Resolve a profile: `[images.<name>]` (with `extends = [...]`), else a combined name
    `a+b` merges each part, else the built-in default. Result: `{from, apt, run}`."""
    images = config.images if config else {}
    base = str((images.get("base") or {}).get("from") or DEFAULT_BASE)
    merged: dict[str, Any] = {"from": None, "apt": [], "run": []}
    for d in _profile_layers(profile, images, frozenset()):
        if d.get("from"):  # a later layer (the extending profile) overrides
            merged["from"] = str(d["from"])
        for pkg in [*(d.get("apt") or []), *(d.get("packages") or [])]:
            if pkg not in merged["apt"]:
                merged["apt"].append(str(pkg))
        merged["run"] += [str(x) for x in d.get("run") or []]
    merged["from"] = merged["from"] or base
    return merged


def profile_dockerfile(definition: dict[str, Any]) -> str:
    lines = [f"FROM {definition['from']}", "USER root"]
    if definition["apt"]:
        pkgs = " ".join(sorted(definition["apt"]))
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{pkgs} && rm -rf /var/lib/apt/lists/*"
        )
    lines += [f"RUN {cmd}" for cmd in definition["run"]]
    return "\n".join(lines) + "\n"


def resolve_image(
    image: str,
    repo: RepoConfig | None,
    config: Config | None,
    worktree: Path,
) -> ImageChoice:
    repo_name = repo.name if repo else worktree.name
    markers = detect_markers(worktree)
    if image and image != "auto":
        return ImageChoice(image, "explicit", markers=markers)
    if repo and repo.image:
        return ImageChoice(repo.image, "repo", markers=markers)
    if repo and repo.image_profile:
        return _profile_choice(repo.image_profile, repo_name, config, markers, "repo")
    dev = devcontainer_image(worktree)
    if dev:
        return ImageChoice(dev, "devcontainer", markers=markers)
    df = worktree / "Dockerfile.agentline"
    if df.is_file():
        text = df.read_text(errors="replace", encoding="utf-8")
        tag = image_tag(repo_name, {"dockerfile.agentline": text})
        return ImageChoice(tag, "dockerfile", None, markers, text, worktree)
    profile = profile_for_markers(markers)
    if profile:
        return _profile_choice(profile, repo_name, config, markers, "profile")
    base = str((config.images.get("base", {}) if config else {}).get("from") or DEFAULT_BASE)
    return ImageChoice(base, "base", markers=markers)


def _profile_choice(
    profile: str, repo: str, config: Config | None, markers: list[str], source: str
) -> ImageChoice:
    text = profile_dockerfile(profile_definition(profile, config))
    tag = image_tag(repo, {"profile": profile, "dockerfile": text})
    return ImageChoice(tag, source, profile, markers, text, None)


async def build_image(
    choice: ImageChoice,
    *,
    docker: str = "docker",
    executor: Executor | None = None,
    timeout: float | None = 1800,
) -> dict[str, Any]:
    """Build `choice.ref` unless `docker image inspect` finds it. Idempotent."""
    run = executor or run_process
    env = base_env()
    code, _, _ = await run([docker, "image", "inspect", choice.ref], env=env, timeout=60)
    if code == 0 or not choice.needs_build:
        return {"tag": choice.ref, "built": False}
    with tempfile.TemporaryDirectory(prefix="agentline-build-") as empty:
        context = str(choice.context or empty)
        code, _, err = await run(
            [docker, "build", "-t", choice.ref, "-f", "-", context],
            env=env,
            input=choice.dockerfile,
            timeout=timeout,
        )
    if code != 0:
        raise RuntimeError(f"docker build {choice.ref} failed ({code}): {err.strip()[-800:]}")
    return {"tag": choice.ref, "built": True}


# -- missing tools -> image proposal ---------------------------------------------------------
APT_FOR_COMMAND: dict[str, str] = {
    "psql": "postgresql-client",
    "pg_dump": "postgresql-client",
    "pg_isready": "postgresql-client",
    "mysql": "default-mysql-client",
    "redis-cli": "redis-tools",
    "sqlite3": "sqlite3",
    "protoc": "protobuf-compiler",
    "jq": "jq",
    "yq": "yq",
    "make": "make",
    "cmake": "cmake",
    "gcc": "gcc",
    "g++": "g++",
    "cc": "gcc",
    "clang": "clang",
    "pkg-config": "pkg-config",
    "node": "nodejs",
    "nodejs": "nodejs",
    "npm": "npm",
    "python": "python-is-python3",
    "python3": "python3",
    "pip": "python3-pip",
    "pip3": "python3-pip",
    "go": "golang-go",
    "cargo": "cargo",
    "rustc": "rustc",
    "java": "default-jdk-headless",
    "javac": "default-jdk-headless",
    "mvn": "maven",
    "ruby": "ruby",
    "curl": "curl",
    "wget": "wget",
    "git": "git",
    "rg": "ripgrep",
    "unzip": "unzip",
    "zip": "zip",
    "xz": "xz-utils",
    "ssh": "openssh-client",
    "rsync": "rsync",
    "shellcheck": "shellcheck",
    "ffmpeg": "ffmpeg",
    "convert": "imagemagick",
    "dot": "graphviz",
}

Proposer = Callable[[list[str], str | None, list[str]], dict[str, Any]]


def propose_image_change(
    missing_tools: list[str], profile: str | None, markers: list[str]
) -> dict[str, Any]:
    """Deterministic proposal: map each missing command to an apt package.

    Returns `{profile_from, profile_to, add_apt, reason, unknown}` (+ `toml`, a snippet to
    add to agentline.toml). Nothing is applied here; `agentline approve --proposal` does that.
    """
    profile_from = profile or profile_for_markers(markers) or "base"
    add_apt: list[str] = []
    known: list[str] = []
    unknown: list[str] = []
    for tool in dict.fromkeys(missing_tools):
        pkg = APT_FOR_COMMAND.get(tool)
        if pkg is None:
            unknown.append(tool)
            continue
        known.append(tool)
        if pkg not in add_apt:
            add_apt.append(pkg)
    profile_to = f"{profile_from}+{'+'.join(sorted(known))}" if known else profile_from
    reason = (
        "; ".join(f"`{t}` is provided by apt package {APT_FOR_COMMAND[t]}" for t in known)
        or "no known package for the missing tools"
    )
    if unknown:
        reason += f"; no known package for: {', '.join(unknown)} (needs a human)"
    toml = None
    if add_apt:
        extends = [] if profile_from == "base" else [profile_from]
        toml = (
            f'[images."{profile_to}"]\n'
            f"extends = {json.dumps(extends)}\n"
            f"apt = {json.dumps(sorted(add_apt))}\n"
        )
    return {
        "profile_from": profile_from,
        "profile_to": profile_to,
        "add_apt": add_apt,
        "reason": reason,
        "unknown": unknown,
        "toml": toml,
        "proposer": "deterministic",
    }


_proposer: Proposer = propose_image_change


def set_proposer(fn: Proposer) -> Proposer:
    """Swap the proposer (e.g. an LLM-backed `image.propose`); returns the previous one."""
    global _proposer
    prev, _proposer = _proposer, fn
    return prev


def propose(missing_tools: list[str], profile: str | None, markers: list[str]) -> dict[str, Any]:
    return _proposer(missing_tools, profile, markers)
