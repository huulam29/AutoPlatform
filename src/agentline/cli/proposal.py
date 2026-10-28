"""`agentline approve --proposal`: apply an image proposal to the local agentline.toml only
(decision 9), with a minimal TOML section writer that leaves the rest of the file untouched.

The writer replaces the body of one `[images.<profile>]` table (keeping its header line), or
appends the table at the end. Everything outside that table body stays byte-identical. The
result is parsed back with `tomllib` and must equal the old config with only that table
changed, otherwise nothing is written. A copy of the old file goes to `agentline.toml.bak`.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentline.config import Config
from agentline.errors import ValidationError
from agentline.ids import canonical_json
from agentline.ledger.base import Execution
from agentline.tomlwrite import header_keys, set_table  # noqa: F401 (re-exported)
from agentline.workspaces.images import (
    DEFAULT_BASE,
    ImageChoice,
    image_tag,
    profile_definition,
    profile_dockerfile,
)


# -- proposal ids -------------------------------------------------------------------------------
def proposal_id(ex: Execution) -> str | None:
    """Stable id: `p-<execution id>-<sha256(canonical proposal)[:8]>`."""
    prop = (ex.detail or {}).get("image_proposal")
    if not isinstance(prop, dict):
        return None
    digest = hashlib.sha256(canonical_json(prop).encode()).hexdigest()[:8]
    return f"p-{ex.id}-{digest}"


def find_proposal(execs: list[Execution], pid: str) -> tuple[Execution, dict[str, Any]]:
    for ex in execs:
        if proposal_id(ex) == pid:
            if ex.status != "needs-review" or ex.reason != "missing_tools":
                raise ValidationError(
                    f"proposal {pid} belongs to {ex.key}, which is {ex.status}"
                    f" ({ex.reason or 'no reason'}), not needs-review: missing_tools"
                )
            return ex, dict(ex.detail["image_proposal"])
    known = [p for x in execs if (p := proposal_id(x))]
    raise ValidationError(
        f"no proposal {pid} in this run" + (f" (have: {', '.join(known)})" if known else "")
    )


# -- TOML writing: see agentline.tomlwrite ----------------------------------------------------
def write_images_table(
    text: str, profile: str, table: dict[str, Any], comment: str | None = None
) -> str:
    """Return `text` with `[images.<profile>]`'s body replaced by `table`, or the table
    appended. Raises ValidationError when that can't be done safely."""
    flat = {k: v for k, v in table.items() if not isinstance(v, dict)}
    return set_table(text, "images", profile, flat, comment)


# -- applying a proposal ------------------------------------------------------------------------
@dataclass
class Applied:
    path: Path
    backup: Path | None
    profile: str
    table: dict[str, Any]
    diff: str
    created: bool


def proposed_table(config: Config, proposal: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The `[images.<profile>]` table after the proposal: the existing table (or the resolved
    built-in definition) with the proposal's apt packages added."""
    profile = str(proposal.get("profile_from") or "base")
    add = [str(x) for x in proposal.get("add_apt") or []]
    if not add:
        unknown = ", ".join(proposal.get("unknown") or []) or "none"
        raise ValidationError(
            f"the proposal has no packages to add (unknown tools: {unknown}); "
            "write the image change by hand"
        )
    if profile == "base":
        raise ValidationError(
            "the proposal extends the base image, which has no toolchain profile; set "
            "image_profile for the repo and add the packages to [images.<profile>] by hand"
        )
    existing = config.raw.get("images", {}).get(profile)
    if isinstance(existing, dict):
        table = dict(existing)
        apt = [str(x) for x in table.get("apt") or []]
    else:
        definition = profile_definition(profile, config)
        base = str((config.images.get("base") or {}).get("from") or DEFAULT_BASE)
        table = {}
        if definition["from"] != base:
            table["from"] = definition["from"]
        apt = list(definition["apt"])
        if definition["run"]:
            table["run"] = list(definition["run"])
    for pkg in add:
        if pkg not in apt:
            apt.append(pkg)
    table["apt"] = apt
    return profile, table


def apply_proposal(config: Config, path: Path, proposal: dict[str, Any], pid: str) -> Applied:
    profile, table = proposed_table(config, proposal)
    created = not path.exists()
    old = "" if created else path.read_text()
    comment = f"apt += {', '.join(proposal.get('add_apt') or [])} (agentline approve {pid})"
    new = write_images_table(old, profile, table, comment)
    backup = None
    if not created:
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(new)
    os.replace(tmp, path)
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{path} (before)",
            tofile=str(path),
        )
    )
    return Applied(path, backup, profile, table, diff, created)


def image_choice(
    config: Config, profile: str, repo: str | None, markers: list[str]
) -> ImageChoice | None:
    """The image the container provider will resolve for this profile after the change
    (same tag computation as `images._profile_choice`)."""
    if not repo:
        return None
    text = profile_dockerfile(profile_definition(profile, config))
    tag = image_tag(repo, {"profile": profile, "dockerfile": text})
    return ImageChoice(tag, "repo", profile, markers, text, None)


def repo_of(ex: Execution, config: Config) -> str | None:
    ws = (ex.detail or {}).get("workspace") or {}
    repo = (ws.get("options") or {}).get("repo") if isinstance(ws, dict) else None
    if repo:
        return str(repo)
    return next(iter(config.repos)) if len(config.repos) == 1 else None
