"""Which files in a pipelines folder are pipelines: every `*.yaml` / `*.yml`, at the top or in
a pipeline's own folder (`<name>/pipeline.yaml`, as the Chat, imports and templates write
them), skipping dot folders (history) and tool folders. The engine, the CLI and the editor use
this one rule, so what the editor lists is what the engine runs."""

from __future__ import annotations

from pathlib import Path

SKIP_DIRS = {"node_modules", ".git", ".venv", "__pycache__", "dist"}


def pipeline_files(root: str | Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    real = root.resolve()
    out = []
    for p in sorted([*root.rglob("*.yaml"), *root.rglob("*.yml")]):
        rel = p.relative_to(root).parts
        if any(x in SKIP_DIRS or x.startswith(".") for x in rel[:-1]):
            continue
        if p.is_file() and p.resolve().is_relative_to(real):
            out.append(p)
    return out
