"""Pipeline import / export as a .zip bundle.

    <folder>/pipeline.yaml            the pipeline and the files next to it (prompts, schemas,
    <folder>/prompts/...              scripts)
    agentline-export.json             manifest: what it needs (harnesses, models, model sources,
                                      MCP servers, skills, key names) and the non-built-in
                                      settings tables for them. Never a key value.

A pipeline in its own folder exports the whole folder; one at the top of the pipelines folder
exports itself plus the files it refers to. Import writes into `<pipelines>/<folder>/` (never
over an existing folder unless asked), validates, and reports what's missing here; with
`apply_settings` it adds the bundle's settings tables that aren't configured yet.
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from agentline import agents as ai
from agentline.config import Config
from agentline.errors import ValidationError
from agentline.pipeline.spec import PipelineSpec, parse_spec

MANIFEST = "agentline-export.json"
FORMAT = 1
MAX_BUNDLE = 20_000_000
MAX_FILE = 5_000_000
MAX_FILES = 500
_SKIP = {"__pycache__", ".git", "node_modules", ".venv", ".DS_Store", "__MACOSX"}
_FOLDER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def _write(path: Path, text: str) -> None:
    """Atomic text write (temp file in the same folder, then rename)."""
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class LocalFiles:
    """What `import_pipeline` needs from the editor's PipelineFiles, without FastAPI (for the
    CLI): root, config, registry, lock, model(), name_of()."""

    def __init__(self, root: Path, registry: Any, config: Config | None) -> None:
        import threading

        self.root = Path(root).resolve()
        self.registry = registry
        self.config = config
        self.lock = threading.Lock()

    def model(self, text: str, path: Path) -> dict[str, Any]:
        from agentline.pipeline.roundtrip import to_model

        return to_model(
            text, path=path, base_dir=path.parent, registry=self.registry, config=self.config
        )

    def name_of(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix().removesuffix(".yaml")


def requirements(spec: PipelineSpec, cfg: Config | None) -> dict[str, Any]:
    """Names of what the pipeline uses, and the configured (non-built-in) tables behind them."""
    harnesses: set[str] = set()
    models: set[str] = set()
    mcp: set[str] = set()
    skills: set[str] = set()
    d = spec.defaults
    known = ai.harnesses(cfg)

    def use(harness_ref: str | None, model_ref: str | None) -> None:
        """What a node actually runs with (defaults are a pair; see agents.choose)."""
        harness, model = ai.choose(harness_ref, model_ref, d.harness, d.model)
        harness = ai.harness_name(harness, cfg) or ("builtin" if model else ai.FALLBACK_HARNESS)
        harnesses.add(harness)
        if model is None and harness in known and known[harness].model:
            model = known[harness].model
        if model:
            models.add(str(model))

    for n in spec.nodes.values():
        if n.type == "agent" and not n.runner:
            use(n.harness, n.model)
        elif n.type == "router" and n.router_mode == "llm":
            models.add(str(n.model or d.model or ""))
        mcp |= set(n.mcp)
        skills |= set(n.skills)
    models.discard("")
    known_models = ai.models(cfg)
    providers = {known_models[m].provider for m in models if m in known_models}
    providers |= {m.split(":", 1)[0] for m in models if ":" in m and m not in known_models}
    provs = ai.providers(cfg)
    keys = {provs[p].api_key_env for p in providers if p in provs and provs[p].api_key_env}
    if cfg:
        keys |= {k for name in mcp if name in cfg.mcp for k in cfg.mcp[name].key_names()}
    raw = cfg.raw if cfg else {}
    raw_harnesses = {**raw.get("agents", {}), **raw.get("harnesses", {})}
    tables = {
        "harnesses": {a: raw_harnesses[a] for a in harnesses if a in raw_harnesses},
        "models": {m: raw["models"][m] for m in models if m in raw.get("models", {})},
        "providers": {p: raw["providers"][p] for p in providers if p in raw.get("providers", {})},
        "mcp": {m: raw["mcp"][m] for m in mcp if m in raw.get("mcp", {})},
    }
    return {
        "harnesses": sorted(harnesses),
        "models": sorted(models),
        "providers": sorted(providers),
        "mcp": sorted(mcp),
        "skills": sorted(skills),
        "keys": sorted(keys),
        "settings": {k: v for k, v in tables.items() if v},
    }


def _referenced(text: str, base: Path, root: Path) -> list[Path]:
    """Files a top-level pipeline mentions: prompt / output_schema paths and scripts/…"""
    out: set[Path] = set()
    try:
        data = YAML(typ="safe").load(text) or {}
    except YAMLError:
        data = {}
    for node in (data.get("nodes") or {}).values() if isinstance(data, dict) else []:
        if not isinstance(node, dict):
            continue
        for key in ("prompt", "output_schema"):
            v = node.get(key)
            if isinstance(v, str) and "\n" not in v and len(v) < 300:
                out.add(base / v)
    for m in re.finditer(r"(?:scripts|prompts|schemas)/[A-Za-z0-9_./-]+", text):
        out.add(base / m.group(0))
    return sorted(p for p in out if p.is_file() and p.resolve().is_relative_to(root.resolve()))


def export_bundle(root: Path, path: Path, cfg: Config | None) -> tuple[bytes, str]:
    """(zip bytes, file name) for the pipeline file `path` under `root`."""
    root = root.resolve()
    path = path.resolve()
    text = path.read_text()
    spec = parse_spec(text, path=path)
    base = path.parent
    if base != root:
        folder = base.name
        files = [
            p
            for p in sorted(base.rglob("*"))
            if p.is_file()
            and not any(x in _SKIP or x.startswith(".") for x in p.relative_to(base).parts)
        ]
    else:
        folder = path.stem
        files = [path, *_referenced(text, base, root)]
    if len(files) > MAX_FILES:
        raise ValidationError(f"{len(files)} files is too many to export (max {MAX_FILES})")
    from agentline import __version__

    manifest = {
        "format": FORMAT,
        "pipeline": spec.name,
        "file": f"{folder}/{path.relative_to(base).as_posix()}",
        "exported_at": time.time(),
        "agentline": __version__,
        "requires": requirements(spec, cfg),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, json.dumps(manifest, indent=2))
        for p in files:
            z.write(p, f"{folder}/{p.relative_to(base).as_posix()}")
    return buf.getvalue(), f"{folder}.agentline.zip"


def read_bundle(data: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """(manifest, {relative path: bytes}) after the safety checks."""
    if len(data) > MAX_BUNDLE:
        raise ValidationError("the bundle is larger than 20 MB")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValidationError("not a .zip file") from None
    try:
        manifest = json.loads(z.read(MANIFEST))
    except KeyError:
        raise ValidationError(f"not an agentline export (no {MANIFEST})") from None
    except ValueError:
        raise ValidationError(f"{MANIFEST} isn't valid JSON") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ValidationError(f"unsupported export format {manifest.get('format')!r}")
    files: dict[str, bytes] = {}
    for info in z.infolist():
        name = info.filename
        if info.is_dir() or name == MANIFEST:
            continue
        parts = PurePosixPath(name).parts
        if (
            name.startswith("/")
            or ".." in parts
            or any(x in _SKIP for x in parts)
            or (info.external_attr >> 16) & 0o170000 == 0o120000  # a symlink
        ):
            raise ValidationError(f"unsafe path in the bundle: {name!r}")
        if info.file_size > MAX_FILE:
            raise ValidationError(f"{name} is larger than 5 MB")
        files[name] = z.read(info)
    if len(files) > MAX_FILES:
        raise ValidationError("too many files in the bundle")
    if str(manifest.get("file")) not in files:
        raise ValidationError(f"the bundle has no {manifest.get('file')!r}")
    return manifest, files


def missing(requires: dict[str, Any], cfg: Config | None) -> dict[str, list[str]]:
    import os

    from agentline.skills import SkillLibrary

    have_skills = {s.name for s in SkillLibrary().list()}
    return {
        "harnesses": [
            a
            for a in [*requires.get("harnesses", []), *requires.get("agents", [])]
            if ai.harness_name(a, cfg) not in ai.harnesses(cfg)
        ],
        "models": [
            m for m in requires.get("models", []) if m not in ai.models(cfg) and ":" not in m
        ],
        "providers": [p for p in requires.get("providers", []) if p not in ai.providers(cfg)],
        "mcp": [m for m in requires.get("mcp", []) if not (cfg and m in cfg.mcp)],
        "keys": [k for k in requires.get("keys", []) if not os.environ.get(k)],
        "skills": [s for s in requires.get("skills", []) if s not in have_skills],
    }


def import_bundle(
    root: Path,
    data: bytes,
    *,
    folder: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write the bundle under `root/<folder>/`. Returns {folder, file, manifest}."""
    manifest, files = read_bundle(data)
    top = str(manifest["file"]).split("/", 1)[0]
    folder = folder or top
    if not _FOLDER.match(folder):
        raise ValidationError(f"bad folder name {folder!r}")
    dest = (root / folder).resolve()
    if not dest.is_relative_to(root.resolve()) or dest == root.resolve():
        raise ValidationError(f"{folder!r} is outside the pipelines folder")
    if dest.exists() and not overwrite:
        raise FileExistsError(folder)
    for name, content in files.items():
        first, _, rest = name.partition("/")
        if first != top or not rest:
            continue  # only the pipeline's folder
        target = (dest / rest).resolve()
        if not target.is_relative_to(dest):
            raise ValidationError(f"unsafe path in the bundle: {name!r}")
        if target.suffix in (".yaml", ".yml", ".md", ".json", ".txt", ".sh", ".py"):
            _write(target, content.decode("utf-8", errors="replace"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if rest.startswith("scripts/") or content.startswith(b"#!"):
            target.chmod(0o755)
    file = f"{folder}/{str(manifest['file']).split('/', 1)[1]}"
    return {"folder": folder, "file": file, "manifest": manifest}


SETTINGS_ORDER = ("providers", "models", "mcp", "harnesses")


def _config_path(cfg: Config | None) -> Path:
    import os

    if cfg is not None and cfg.path is not None:
        return Path(cfg.path)
    return Path(os.environ.get("AGENTLINE_CONFIG") or "agentline.toml").resolve()


def add_settings(cfg: Config | None, tables: dict[str, dict[str, Any]]) -> tuple[list[str], Path]:
    """Add the bundle's settings tables that aren't configured here (never secret-looking
    ones). Returns (added "section.name"s, the config file)."""
    import tomllib

    from agentline.config import parse_harness, parse_model, parse_provider
    from agentline.mcp import parse_mcp
    from agentline.tomlwrite import set_table, write_file

    parsers = {
        "providers": parse_provider,
        "models": parse_model,
        "harnesses": parse_harness,
        "mcp": parse_mcp,
    }
    cfg = cfg or Config()
    if "agents" in tables:  # a bundle made before phase 35
        tables = {**tables, "harnesses": {**tables["agents"], **(tables.get("harnesses") or {})}}
    path = _config_path(cfg)
    text = path.read_text() if path.exists() else ""
    added = []
    for section in SETTINGS_ORDER:
        for name, table in (tables.get(section) or {}).items():
            if name in getattr(cfg, section) or not isinstance(table, dict):
                continue
            if any(k in table for k in ("api_key", "key", "token", "secret")):
                continue
            parsers[section](name, table)
            text = set_table(text, section, name, table, comment="added by pipeline import")
            added.append(f"{section}.{name}")
    if added:
        Config.from_dict(tomllib.loads(text), path)  # the whole file still loads
        write_file(path, text)
        from agentline import restart

        restart.note("settings added by an import")
    return added, path


def import_pipeline(
    files: Any,
    data: bytes,
    *,
    folder: str | None = None,
    overwrite: bool = False,
    apply_settings: bool = False,
) -> dict[str, Any]:
    """Import into the editor's PipelineFiles and report: {name, pipeline, issues, missing,
    settings, applied}. Raises FileExistsError or ValidationError."""
    from agentline import history

    with files.lock:
        history.record(files.root, "before import")
        out = import_bundle(files.root, data, folder=folder, overwrite=overwrite)
        history.record(files.root, f"import {out['file']}")
    requires = out["manifest"].get("requires") or {}
    applied: list[str] = []
    if apply_settings:
        applied, path = add_settings(files.config, requires.get("settings") or {})
        if applied:
            from agentline.builtins import default_registry

            files.config = Config.load(path)
            files.registry = default_registry(files.config)
    path = files.root / out["file"]
    model = files.model(path.read_text(), path)
    return {
        "name": files.name_of(path),
        "pipeline": out["manifest"].get("pipeline"),
        "issues": model.get("issues", []),
        "missing": missing(requires, files.config),
        "settings": requires.get("settings") or {},
        "applied": applied,
    }
