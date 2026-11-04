"""Pipeline templates shipped with agentline (package data), used by `agentline new`.

Each template is a directory here with a `pipeline.yaml` (its top-level `description:` is what
`agentline templates` shows) plus any prompts and schemas it refers to by relative path.
`copy_template` writes the pipeline as `<dest>/<name>.yaml`, so a pipelines directory can hold
several pipelines, and copies the other files next to it with their relative paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from agentline.errors import ValidationError

PIPELINE_FILE = "pipeline.yaml"
_NAME_LINE = re.compile(r"^name:[^\n]*$", re.M)
_DESCRIPTION = re.compile(r"^description:\s*(.*)$", re.M)
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Template:
    name: str
    description: str
    root: Traversable

    def files(self) -> list[tuple[str, Traversable]]:
        """(relative path, file) for every file in the template, the pipeline first."""
        out: list[tuple[str, Traversable]] = []

        def walk(node: Traversable, prefix: str) -> None:
            for child in sorted(node.iterdir(), key=lambda c: c.name):
                rel = f"{prefix}{child.name}"
                if child.is_dir():
                    if child.name != "__pycache__":
                        walk(child, rel + "/")
                elif rel != PIPELINE_FILE:
                    out.append((rel, child))

        walk(self.root, "")
        return [(PIPELINE_FILE, self.root / PIPELINE_FILE), *out]


def _root() -> Traversable:
    return resources.files(__name__)


def list_templates() -> list[Template]:
    out: list[Template] = []
    for d in sorted(_root().iterdir(), key=lambda c: c.name):
        if not d.is_dir() or not (d / PIPELINE_FILE).is_file():
            continue
        m = _DESCRIPTION.search((d / PIPELINE_FILE).read_text(encoding="utf-8"))
        desc = m.group(1).strip().strip("'\"") if m else ""
        out.append(Template(d.name, desc, d))
    return out


def get_template(name: str) -> Template:
    for t in list_templates():
        if t.name == name:
            return t
    known = ", ".join(t.name for t in list_templates())
    raise ValidationError(f"unknown template {name!r} (known: {known})")


def render_pipeline(text: str, name: str) -> str:
    """The template's pipeline YAML with its top-level `name:` set to `name`."""
    if not _NAME_LINE.search(text):
        raise ValidationError("template pipeline has no top-level 'name:' line")
    return _NAME_LINE.sub(lambda _: f"name: {name}", text, count=1)


def copy_template(
    template: str, dest: str | Path, *, name: str | None = None, force: bool = False
) -> list[Path]:
    """Copy a template into its own folder, `dest/<name>/pipeline.yaml` plus its prompts,
    schemas and scripts (the layout the Chat and imports use, so pipelines never share files).
    Refuses to overwrite any existing file unless `force`; nothing is written when it refuses.
    Returns the files written, the pipeline first."""
    t = get_template(template)
    pipeline_name = name or t.name
    if not _VALID_NAME.match(pipeline_name):
        raise ValidationError(
            f"invalid pipeline name {pipeline_name!r}: use letters, digits, '-', '_' or '.'"
        )
    folder = Path(dest) / pipeline_name
    plan: list[tuple[Path, bytes]] = []
    for rel, src in t.files():
        if rel == PIPELINE_FILE:
            data = render_pipeline(src.read_text(encoding="utf-8"), pipeline_name).encode()
            plan.insert(0, (folder / "pipeline.yaml", data))
        else:
            plan.append((folder / rel, src.read_bytes()))
    existing = [p for p, _ in plan if p.exists()]
    if existing and not force:
        names = ", ".join(str(p) for p in existing)
        raise ValidationError(f"refusing to overwrite {names} (use --force)")
    for path, data in plan:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return [p for p, _ in plan]


__all__ = ["Template", "copy_template", "get_template", "list_templates", "render_pipeline"]
