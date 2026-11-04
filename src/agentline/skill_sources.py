"""Installing skills published by others: the skills.sh marketplace and GitHub repos.

Works like `npx skills add docker/skills` (the `skills` npm CLI), natively, without Node:

- a source is a GitHub repo: `owner/repo`, `owner/repo/sub/path`, `owner/repo@skill`,
  `owner/repo#ref`, a `https://github.com/owner/repo[/tree/ref/path]` URL, or a pasted
  `npx skills add owner/repo [--skill name]` command;
- the repo is downloaded as one tarball (codeload.github.com) and searched for `SKILL.md`
  folders the way the CLI does: the root, `skills/` (and its `.curated` / `.experimental` /
  `.system` groups), the agents' `.claude/skills`-style folders, and only when those have none,
  the whole tree (5 levels deep);
- the chosen skills are copied to `<skills>/installed/<name>/`, and where each came from
  (repo, ref, path, commit) is kept in `<skills>/installed.json` so it can be updated or removed.

Search goes to skills.sh's `/api/search` (what `npx skills find` uses). Both URLs can be
pointed elsewhere (`AGENTLINE_SKILLS_API`, `AGENTLINE_GITHUB_CODELOAD`) for tests.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError
from agentline.fsutil import rmtree
from agentline.skills import NAME, SkillLibrary

MAX_TARBALL = 50_000_000
MAX_FILE = 5_000_000
MAX_SKILL_FILES = 500
CACHE_TTL = 600.0  # a preview's download is reused by the install that follows it

SKIP_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__"}
# where agents keep project skills (a subset of the CLI's list: the common ones)
AGENT_SKILL_DIRS = [
    ".agents/skills", ".claude/skills", ".codex/skills", ".cursor/skills", ".github/skills",
    ".opencode/skills", ".gemini/skills", ".windsurf/skills", ".cline/skills", ".roo/skills",
    ".goose/skills", ".kiro/skills",
]  # fmt: skip
PART = re.compile(r"^[A-Za-z0-9_.-]+$")


def search_api() -> str:
    return (os.environ.get("AGENTLINE_SKILLS_API") or "https://skills.sh").rstrip("/")


def codeload() -> str:
    return (os.environ.get("AGENTLINE_GITHUB_CODELOAD") or "https://codeload.github.com").rstrip(
        "/"
    )


# -- sources --------------------------------------------------------------------------------
@dataclass(frozen=True)
class Source:
    owner: str
    repo: str
    ref: str = "HEAD"  # the default branch
    path: str = ""  # a folder inside the repo
    skill: str = ""  # only this skill (`owner/repo@skill`, `--skill`)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def label(self) -> str:
        """The canonical spelling (parse_source reads it back)."""
        out = self.slug + (f"/{self.path}" if self.path else "")
        out += f"#{self.ref}" if self.ref != "HEAD" else ""
        return out + (f"@{self.skill}" if self.skill else "")

    @property
    def url(self) -> str:
        return f"https://github.com/{self.slug}"


def _bad(text: str) -> ValidationError:
    return ValidationError(
        f"can't read {text!r} as a skills source: use owner/repo (like docker/skills), "
        "owner/repo/path, owner/repo@skill, or a github.com URL"
    )


def parse_source(text: str) -> Source:
    raw = " ".join(str(text).split())
    words = raw.split(" ")
    # a pasted `npx skills add owner/repo --skill x` command
    for prefix in (["npx", "-y"], ["npx"], ["bunx"], ["pnpx"], ["pnpm", "dlx"]):
        if words[: len(prefix)] == prefix:
            words = words[len(prefix) :]
            break
    if words[:1] == ["skills"]:
        words = words[1:]
    if words[:1] in (["add"], ["install"]):
        words = words[1:]
    skill, rest = "", []
    i = 0
    while i < len(words):
        w = words[i]
        if w in ("--skill", "-s") and i + 1 < len(words):
            skill = words[i + 1]
            i += 2
            continue
        if not w.startswith("-"):
            rest.append(w)
        i += 1
    if len(rest) != 1:
        raise _bad(raw)
    s = rest[0]
    if s.startswith("github:"):
        s = s[len("github:") :]
    ref, path = "HEAD", ""
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/(.+)$", s)
    if m:
        bits = [b for b in m.group(1).split("#")[0].split("/") if b]
        if len(bits) < 2:
            raise _bad(raw)
        owner, repo = bits[0], bits[1].removesuffix(".git")
        if len(bits) >= 4 and bits[2] in ("tree", "blob"):
            ref, path = bits[3], "/".join(bits[4:])
            if path.endswith("/SKILL.md") or path == "SKILL.md":
                path = path.rsplit("SKILL.md", 1)[0].rstrip("/")
        elif len(bits) > 2:
            raise _bad(raw)
        if "#" in m.group(1):
            ref = m.group(1).split("#", 1)[1] or ref
    else:
        if "@" in s:
            s, at = s.split("@", 1)
            skill = skill or at
        if "#" in s:
            s, ref = s.split("#", 1)
        bits = [b for b in s.split("/") if b]
        if len(bits) < 2:
            raise _bad(raw)
        owner, repo, path = bits[0], bits[1].removesuffix(".git"), "/".join(bits[2:])
    parts = [owner, repo, *[p for p in path.split("/") if p]]
    if not all(PART.match(p) and p not in (".", "..") for p in parts):
        raise _bad(raw)
    if not re.match(r"^[A-Za-z0-9_./-]+$", ref) or ".." in ref:
        raise _bad(raw)
    return Source(owner, repo, ref or "HEAD", path.strip("/"), skill.strip())


# -- download + unpack ----------------------------------------------------------------------
_cache: dict[tuple[str, str], tuple[float, bytes]] = {}
_cache_lock = threading.Lock()


def download(src: Source, timeout: float = 60, fresh: bool = False) -> bytes:
    key = (src.slug.lower(), src.ref)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not fresh and now - hit[0] < CACHE_TTL:
            return hit[1]
    url = f"{codeload()}/{src.owner}/{src.repo}/tar.gz/{urllib.parse.quote(src.ref)}"
    req = urllib.request.Request(url, headers={"user-agent": "agentline"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_TARBALL + 1)
    if len(data) > MAX_TARBALL:
        raise ValidationError(f"{src.slug} is too big to download (over 50 MB)")
    with _cache_lock:
        for k in [k for k, (t, _) in _cache.items() if now - t >= CACHE_TTL]:
            del _cache[k]
        _cache[key] = (now, data)
    return data


def unpack(data: bytes, dest: Path) -> str:
    """Regular files only, nothing outside `dest`; the top folder is dropped. Returns the
    commit id GitHub puts in the tarball's header ("" if none)."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")  # noqa: SIM115 (closed below)
    except (tarfile.TarError, OSError) as e:
        raise ValidationError(f"the download isn't a tarball ({e})") from None
    with tar:
        sha = str(tar.pax_headers.get("comment", "")) if tar.pax_headers else ""
        for m in tar.getmembers():
            parts = Path(m.name).parts
            if len(parts) < 2 or not m.isfile() or ".." in parts or m.name.startswith("/"):
                continue
            if m.size > MAX_FILE:
                continue
            target = dest.joinpath(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            f = tar.extractfile(m)
            if f is not None:
                target.write_bytes(f.read())
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else ""


# -- discovery ------------------------------------------------------------------------------
@dataclass
class Found:
    name: str  # the folder it installs as
    description: str
    path: str  # the skill's folder inside the repo ("" = the repo root)
    dir: Path = field(repr=False)


def _meta(md: Path) -> dict[str, Any]:
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    text = md.read_text(errors="replace", encoding="utf-8")[:20_000]
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        data = YAML(typ="safe", pure=True).load(text[3:end])
    except YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def install_name(name: str, folder: str) -> str:
    """The folder name a skill installs as: its `name:` lowercased with spaces as dashes, or
    the folder's own name when that isn't a valid skill name."""
    for cand in (name, folder):
        n = re.sub(r"\s+", "-", str(cand).strip().lower())
        if NAME.match(n):
            return n
    return ""


def discover(root: Path, src: Source) -> tuple[list[Found], list[dict[str, str]]]:
    """The skills in an unpacked repo (and the SKILL.md files skipped, with why)."""
    root = root.resolve()
    base = (root / src.path).resolve()
    if not base.is_relative_to(root) or not base.is_dir():
        raise ValidationError(f"{src.slug} has no folder {src.path!r}")
    found: list[Found] = []
    skipped: list[dict[str, str]] = []
    seen_md: set[Path] = set()
    names: set[str] = set()

    def add(d: Path) -> bool:
        md = d / "SKILL.md"
        if not md.is_file():
            return False
        if md in seen_md:
            return True
        seen_md.add(md)
        rel = d.relative_to(root).as_posix()
        rel = "" if rel == "." else rel
        meta = _meta(md)
        name, desc = meta.get("name"), meta.get("description")
        if not isinstance(name, str) or not isinstance(desc, str) or not name or not desc:
            skipped.append({"path": rel, "reason": "SKILL.md has no name or description"})
            return True
        n = install_name(name, d.name)
        if not n:
            skipped.append({"path": rel, "reason": f"can't use {name!r} as a skill name"})
        elif n not in names:
            names.add(n)
            found.append(Found(n, " ".join(desc.split()), rel, d))
        return True

    def walk(d: Path, max_depth: int, depth: int = 1) -> None:
        if not d.is_dir():
            return
        for child in sorted(d.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            if add(child) or depth >= max_depth or child.name in SKIP_DIRS:
                continue
            walk(child, max_depth, depth + 1)

    if add(base):  # pointed at one skill
        return _pick(found, src), skipped
    walk(base, 1)
    for sub in ["skills", "skills/.curated", "skills/.experimental", "skills/.system",
                *AGENT_SKILL_DIRS]:  # fmt: skip
        walk(base / sub, 3)
    if not found and not skipped:
        walk(base, 5)
    return _pick(found, src), skipped


def _pick(found: list[Found], src: Source) -> list[Found]:
    if not src.skill or src.skill == "*":
        return found
    want = install_name(src.skill, src.skill)
    out = [f for f in found if f.name == want or Path(f.path).name.lower() == want]
    if not out:
        raise ValidationError(f"{src.slug} has no skill {src.skill!r}")
    return out


# -- the installed list ---------------------------------------------------------------------
def lock_path(lib: SkillLibrary) -> Path:
    return lib.base / "installed.json"


def installed(lib: SkillLibrary) -> dict[str, dict[str, Any]]:
    """name -> {source, repo, ref, path, sha, installed_at} for skills installed here."""
    try:
        data = json.loads(lock_path(lib).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _write_lock(lib: SkillLibrary, data: dict[str, dict[str, Any]]) -> None:
    lib.base.mkdir(parents=True, exist_ok=True)
    tmp = lock_path(lib).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, lock_path(lib))


_install_lock = threading.Lock()


def _copy_in(lib: SkillLibrary, f: Found) -> None:
    files = [p for p in f.dir.rglob("*") if p.is_file() and not p.is_symlink()]
    if len(files) > MAX_SKILL_FILES:
        raise ValidationError(f"{f.name} has too many files ({len(files)})")
    lib.installed_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=lib.installed_dir, prefix=".new-"))
    try:
        for p in files:
            rel = p.relative_to(f.dir)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, tmp / rel)
        dest = lib.installed_dir / f.name
        old = lib.installed_dir / f".old-{f.name}"
        if old.exists():
            rmtree(old)
        if dest.exists():
            os.replace(dest, old)
        os.replace(tmp, dest)
        rmtree(old, ignore_errors=True)
    finally:
        rmtree(tmp, ignore_errors=True)


def _fetch(src: Source, fresh: bool = False) -> tuple[Path, str]:
    """Download and unpack into a temp folder (the caller removes it)."""
    data = download(src, fresh=fresh)
    tmp = Path(tempfile.mkdtemp(prefix="agentline-skills-"))
    try:
        sha = unpack(data, tmp)
    except BaseException:
        rmtree(tmp, ignore_errors=True)
        raise
    return tmp, sha


def preview(text: str, lib: SkillLibrary | None = None) -> dict[str, Any]:
    """The skills a source has, and how each relates to what's here already."""
    lib = lib or SkillLibrary()
    src = parse_source(text)
    tmp, sha = _fetch(src)
    try:
        found, skipped = discover(tmp, src)
    finally:
        rmtree(tmp, ignore_errors=True)
    have = {s.name: s.source for s in lib.list()}
    mine = installed(lib)
    return {
        "source": src.label,
        "repo": src.slug,
        "url": src.url,
        "ref": src.ref,
        "sha": sha,
        "skills": [
            {
                "name": f.name,
                "description": f.description,
                "path": f.path,
                "exists": have.get(f.name),  # builtin | fetched | installed | local | None
                "installed_from": (mine.get(f.name) or {}).get("repo"),
            }
            for f in found
        ],
        "skipped": skipped,
    }


def install(text: str, names: list[str] | None = None, lib: SkillLibrary | None = None,
            fresh: bool = False) -> dict[str, Any]:  # fmt: skip
    """Install the named skills of a source (all of them when `names` is None)."""
    lib = lib or SkillLibrary()
    src = parse_source(text)
    tmp, sha = _fetch(src, fresh=fresh)
    try:
        found, _ = discover(tmp, src)
        if names is not None:
            unknown = sorted(set(names) - {f.name for f in found})
            if unknown:
                raise ValidationError(f"{src.slug} has no skill(s) {', '.join(unknown)}")
            found = [f for f in found if f.name in names]
        if not found:
            raise ValidationError(f"no skills found in {src.label}")
        with _install_lock:
            lock = installed(lib)
            for f in found:
                _copy_in(lib, f)
                lock[f.name] = {
                    "source": f"{src.slug}/{f.path}".rstrip("/")
                    + (f"#{src.ref}" if src.ref != "HEAD" else ""),
                    "repo": src.slug,
                    "ref": src.ref,
                    "path": f.path,
                    "sha": sha,
                    "installed_at": time.time(),
                }
            _write_lock(lib, lock)
    finally:
        rmtree(tmp, ignore_errors=True)
    return {"source": src.label, "repo": src.slug, "sha": sha,
            "installed": [f.name for f in found]}  # fmt: skip


def update(lib: SkillLibrary | None = None, names: list[str] | None = None) -> dict[str, Any]:
    """Download installed skills again from where they came from (one download per repo)."""
    lib = lib or SkillLibrary()
    lock = installed(lib)
    todo = {n: e for n, e in lock.items() if names is None or n in names}
    unknown = sorted(set(names or []) - set(lock))
    if unknown:
        raise ValidationError(f"not installed from a source: {', '.join(unknown)}")
    updated: list[str] = []
    missing: list[str] = []
    by_repo: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for n, e in todo.items():
        by_repo.setdefault((str(e.get("repo")), str(e.get("ref") or "HEAD")), {})[n] = e
    for (repo, ref), entries in sorted(by_repo.items()):
        src = parse_source(repo + ("" if ref == "HEAD" else f"#{ref}"))
        tmp, sha = _fetch(src, fresh=True)
        try:
            for n, e in sorted(entries.items()):
                one = Source(src.owner, src.repo, src.ref, str(e.get("path") or ""))
                try:
                    found, _ = discover(tmp, one)
                except ValidationError:
                    found = []
                match = next((f for f in found if f.name == n), None)
                if match is None:
                    missing.append(n)
                    continue
                with _install_lock:
                    _copy_in(lib, match)
                    cur = installed(lib)
                    cur[n] = {**e, "sha": sha, "installed_at": time.time()}
                    _write_lock(lib, cur)
                updated.append(n)
        finally:
            rmtree(tmp, ignore_errors=True)
    return {"updated": updated, "missing": missing}


def remove(lib: SkillLibrary, name: str) -> bool:
    d = lib.installed_dir / name
    if not NAME.match(name) or not (d / "SKILL.md").is_file():
        return False
    with _install_lock:
        rmtree(d)
        lock = installed(lib)
        lock.pop(name, None)
        _write_lock(lib, lock)
    return True


# -- the marketplace ------------------------------------------------------------------------
def search(query: str, limit: int = 20, timeout: float = 20) -> list[dict[str, Any]]:
    """skills.sh search: [{id, name, skill, source, installs}], in skills.sh's own ranking."""
    q = query.strip()
    if len(q) < 2:
        return []
    params = urllib.parse.urlencode({"q": q, "limit": max(1, min(int(limit), 50))})
    req = urllib.request.Request(
        f"{search_api()}/api/search?{params}",
        headers={"user-agent": "agentline", "accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read(2_000_000) or b"{}")
    out = []
    for s in data.get("skills") or []:
        if not isinstance(s, dict):
            continue
        source, name = str(s.get("source") or ""), str(s.get("name") or "")
        skill_id = str(s.get("skillId") or name)
        if not source or not name:
            continue
        installs = s.get("installs")
        out.append({
            "id": str(s.get("id") or f"{source}/{skill_id}"),
            "name": name,
            "skill": skill_id,
            "source": source,
            "installs": installs if isinstance(installs, int) else 0,
        })  # fmt: skip
    return out
