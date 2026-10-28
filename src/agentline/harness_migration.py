"""One-time move to "only `builtin` is built in" (phase 44).

Until phase 44, claude-code, codex and aider were built-in harnesses, and an agent node that
named neither a harness nor a model ran on claude-code. Now only `builtin` is built in and it is
the fallback. So that nothing an install already uses breaks, the first start after the update:
- adds `[harnesses.<name>]` to agentline.toml for each of claude-code / codex / aider that a
  pipeline or the Chat's saved choice uses and that isn't configured (the old built-in values);
- adds `defaults: { harness: claude-code }` to a pipeline whose agent nodes relied on the old
  fallback (no harness, no model, no pipeline default), so they keep running on Claude Code.
  The edit keeps comments (pipeline.roundtrip) and is recorded in the pipeline history.
Harnesses nobody uses are not added. It runs once per data folder
(`$AGENTLINE_HOME/state/harnesses-v44.done`) and never raises: a failure is logged and retried
on the next start.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

log = logging.getLogger("agentline.harness_migration")
OLD_FALLBACK = "claude-code"


def marker_path() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "state" / "harnesses-v44.done"


def _relied_on_fallback(doc: Any) -> tuple[set[str], bool]:
    """(harness names the pipeline names, whether an agent node used the old fallback)."""
    names: set[str] = set()
    if not isinstance(doc, dict):
        return names, False
    defaults = doc.get("defaults") if isinstance(doc.get("defaults"), dict) else {}
    d_harness = defaults.get("harness") or defaults.get("agent")
    if d_harness:
        names.add(str(d_harness))
    fallback = False
    nodes = doc.get("nodes") if isinstance(doc.get("nodes"), dict) else {}
    for n in nodes.values():
        if not isinstance(n, dict) or n.get("type") != "agent":
            continue
        h = n.get("harness") or n.get("agent")
        if h:
            names.add(str(h))
        elif (
            not n.get("runner")
            and not n.get("model")
            and not d_harness
            and not defaults.get("model")
        ):
            fallback = True
    return names, fallback


def migrate(config_path: Path, pipelines_dir: Path) -> dict[str, Any]:
    """Run the migration once. Returns {done, harnesses (added), pipelines (given a default)}."""
    out: dict[str, Any] = {"done": False, "harnesses": [], "pipelines": []}
    marker = marker_path()
    if marker.exists():
        return out
    try:
        out = _migrate(Path(config_path), Path(pipelines_dir))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("phase 44: only builtin is a built-in harness\n")
    except Exception:  # never stop the engine over this; the next start tries again
        log.exception("harness migration failed; it will be retried on the next start")
    return out


def _migrate(config_path: Path, pipelines_dir: Path) -> dict[str, Any]:
    from agentline import agents as ai
    from agentline import history, setup
    from agentline.config import Config
    from agentline.pipeline.files import pipeline_files
    from agentline.pipeline.roundtrip import apply_model, load_document, to_model
    from agentline.tomlwrite import set_table, write_file

    used: set[str] = set()
    fallback_files: list[Path] = []
    for f in pipeline_files(pipelines_dir):
        try:
            doc = load_document(f.read_text())
        except Exception:
            continue  # a broken file: the editor reports it
        names, fallback = _relied_on_fallback(doc)
        used |= names
        if fallback:
            fallback_files.append(f)
            used.add(OLD_FALLBACK)
    choice = setup.read_state().get("assistant") or {}
    if choice.get("harness"):
        used.add(str(choice["harness"]))

    cfg = Config.load(config_path) if config_path.exists() else Config(path=config_path)
    text = config_path.read_text() if config_path.exists() else ""
    added = []
    for name in sorted(used):
        preset = ai.HARNESS_PRESETS.get(ai.harness_name(name, cfg) or name)
        if preset is None or preset.name in cfg.harnesses:
            continue
        table = {"kind": preset.kind, "model": preset.model, "description": preset.description}
        table = {k: v for k, v in table.items() if v}
        text = set_table(
            text,
            "harnesses",
            preset.name,
            table,
            comment="added when agentline stopped building it in",
        )
        added.append(preset.name)
    if added:
        Config.from_dict(tomllib.loads(text), config_path)  # the result still loads
        config_path.parent.mkdir(parents=True, exist_ok=True)
        write_file(config_path, text)
        log.info("harness migration: added %s to %s", ", ".join(added), config_path)

    given = []
    for f in fallback_files:
        source = f.read_text()
        model = to_model(source, validate=False)
        model["defaults"] = {**(model.get("defaults") or {}), "harness": OLD_FALLBACK}
        new, _ = apply_model(source, model)
        if new != source:
            history.record(pipelines_dir, "before the harness migration")
            f.write_text(new)
            history.record(pipelines_dir, f"harness migration: default harness {OLD_FALLBACK}")
            given.append(f.relative_to(pipelines_dir).as_posix())
    if given:
        log.info("harness migration: defaults.harness = %s in %s", OLD_FALLBACK, ", ".join(given))
    return {"done": True, "harnesses": added, "pipelines": given}
