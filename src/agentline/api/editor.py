"""Editor API: read, validate and write pipeline YAML files under one directory.

The YAML file is the only source of truth. `PUT .../model` applies a graph model to the file
through `pipeline.roundtrip.apply_model` (minimal, comment-preserving edits); `PUT .../source`
saves raw text. Both save even when the pipeline is invalid and report its issues.

Routes (all under `/api`):
- `GET  /pipelines`                      list `*.yaml` (recursively): name, path, pipeline, issues
- `GET  /pipelines/{name}`               `{name, path, source, model}`
- `PUT  /pipelines/{name}/source`        `{source}` -> `{name, source, model, issues}`
- `PUT  /pipelines/{name}/model`         `{model, base_source}` -> `{source, model, warnings,
                                         issues}`; 409 when the file changed since `base_source`
- `POST /validate`                       `{source, name?, model?}` -> `{model, issues, source,
                                         warnings}` (with `model`: a dry-run apply)
- `GET  /node-types`                     field schema per node type + registry names

A pipeline `name` is its path relative to the directory without `.yaml`, e.g.
`ticket-to-eod/pipeline`. Names resolving outside the directory are rejected (400).
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agentline import history, restart
from agentline.agents import summary as ai_summary
from agentline.config import Config
from agentline.pipeline.files import pipeline_files
from agentline.pipeline.roundtrip import apply_model, to_model
from agentline.pipeline.spec import COMMON_FIELDS, NODE_TYPES, OLD_FIELDS, REQUIRED, TYPE_FIELDS
from agentline.readiness import ready_sources
from agentline.registry import Registry

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*(/[A-Za-z0-9_][A-Za-z0-9_.\-]*)*$")

# How the property panel edits each field. Anything missing falls back to "yaml".
FIELD_KINDS: dict[str, dict[str, Any]] = {
    "type": {"kind": "enum", "options": list(NODE_TYPES)},
    "description": {"kind": "string"},
    "in": {"kind": "list", "help": "input artifacts; suffix ? for optional (review?)"},
    "out": {"kind": "string", "help": "name of the artifact this node writes"},
    "max_attempts": {"kind": "int", "min": 1},
    "timeout": {"kind": "duration", "help": "seconds or 30s / 10m / 2h"},
    "budget_usd": {"kind": "number", "min": 0},
    "harness": {
        "kind": "select",
        "source": "harnesses",
        "help": "harness from Settings (empty: the pipeline default, else claude-code)",
    },
    "runner": {
        "kind": "select",
        "source": "runners",
        "help": "low-level runner (instead of agent)",
    },
    "prompt": {"kind": "prompt", "help": "inline text, or a file path relative to the pipeline"},
    "output_schema": {"kind": "string", "help": "JSON Schema file path (or an inline map)"},
    "allowed_tools": {"kind": "list", "suggest": ["read", "edit", "bash", "web"]},
    "workspace": {"kind": "workspace", "source": "workspaces"},
    "max_turns": {"kind": "int", "min": 1},
    "for_each": {
        "kind": "string",
        "help": "run one task per item of a list, e.g. plan.steps ({{item}} in templates)",
    },
    "max_parallel": {"kind": "int", "min": 1, "help": "for_each: tasks at a time (default 4)"},
    "mcp": {
        "kind": "list",
        "source": "mcp",
        "help": "MCP servers this node may use (Settings → MCP; claude-code and codex)",
    },
    "skills": {
        "kind": "list",
        "source": "skills",
        "help": "agent skills to load (the library, skills installed in Settings → Skills, "
        "or your own)",
    },
    "with": {"kind": "map", "help": "parameters; values may use {{artifact.field}}"},
    "model": {
        "kind": "select",
        "source": "models",
        "help": "a model from Settings, or provider:model-id (empty: the agent's default)",
    },
    "mode": {"kind": "enum", "options": ["switch", "llm"], "help": "llm: a model picks the branch"},
    "uses": {"kind": "select", "source": "actions"},
    "effect": {
        "kind": "map",
        "keys": ["key", "on_unknown"],
        "help": "on_unknown: halt | retry | ai",
    },
    "switch": {"kind": "string", "help": "<artifact>.<field>, e.g. review.state"},
    "approvers": {"kind": "list"},
    "for": {"kind": "select", "source": "waiters"},
    "poll_every": {"kind": "duration"},
}
_COMMON_ORDER = ["description", "in", "out", "max_attempts", "timeout", "budget_usd"]
_TYPE_ORDER: dict[str, list[str]] = {
    "agent": [
        "harness",
        "model",
        "prompt",
        "output_schema",
        "allowed_tools",
        "workspace",
        "max_turns",
        "skills",
        "mcp",
        "for_each",
        "max_parallel",
        "with",
        "runner",
    ],
    "action": ["uses", "with", "effect", "for_each", "max_parallel"],
    "router": ["mode", "switch", "prompt", "model"],
    "gate": ["prompt", "approvers"],
    "wait": ["for", "with", "poll_every"],
    "chat": [
        "harness",
        "model",
        "prompt",
        "output_schema",
        "allowed_tools",
        "max_turns",
        "skills",
        "mcp",
    ],
}


def node_types(registry: Registry, config: Config | None = None) -> dict[str, Any]:
    """Field schema per node type, derived from `spec.TYPE_FIELDS` / `REQUIRED`."""
    from agentline.skills import SkillLibrary

    skill_names = [x.name for x in SkillLibrary().list()]
    types: dict[str, Any] = {}
    for t in NODE_TYPES:
        allowed = (TYPE_FIELDS[t] | COMMON_FIELDS) - {"type"} - OLD_FIELDS
        order = [f for f in [*_TYPE_ORDER.get(t, []), *_COMMON_ORDER] if f in allowed]
        order += sorted(allowed - set(order))
        types[t] = {
            "required": list(REQUIRED[t]),
            "fields": [
                {"name": f, "required": f in REQUIRED[t], **FIELD_KINDS.get(f, {"kind": "yaml"})}
                for f in order
            ],
        }
        for spec in types[t]["fields"]:
            if spec["name"] == "skills":
                spec["suggest"] = skill_names
            if spec["name"] == "mcp":
                spec["suggest"] = sorted(config.mcp) if config else []
    return {
        "types": types,
        "runners": sorted(registry.runners),
        "actions": sorted(registry.actions),
        "deploy_class_actions": sorted(
            n for n, a in registry.actions.items() if getattr(a, "deploy_class", False)
        ),
        "waiters": sorted(registry.waiters),
        "workspaces": sorted(registry.workspaces),
        "edge": {"on_exhausted": ["needs-review", "fail"], "default_when": "default"},
        "ai": ai_summary(config, ready_sources(config)),  # pickers: usable models only
        "harnesses": [a["name"] for a in ai_summary(config)["harnesses"]],
        "models": [m["name"] for m in ai_summary(config)["models"]],
        "defaults": {"fields": ["harness", "model", "max_turns"]},
        "skills": skill_names,
        "mcp": sorted(config.mcp) if config else [],
    }


class SourceBody(BaseModel):
    source: str


class ModelBody(BaseModel):
    model: dict[str, Any]
    base_source: str


class ValidateBody(BaseModel):
    source: str
    name: str | None = None
    model: dict[str, Any] | None = None  # with `model`, `source` is the base it applies to


def _read(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: Path, text: str) -> None:
    """Atomic: temp file in the same directory, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class PipelineFiles:
    """Pipeline files under `root`, with path-safe name resolution."""

    def __init__(
        self, root: str | Path, registry: Registry | None = None, config: Config | None = None
    ) -> None:
        self.root = Path(root).resolve()
        if registry is None:
            from agentline.builtins import default_registry

            registry = default_registry(config)
        self.registry = registry
        self.config = config
        self.lock = threading.Lock()
        self.live: Any = None  # agentline.live.LivePipelines when the engine runs here

    def attach(self, live: Any) -> None:
        """Follow the engine's live reloads (the config and registry the editor validates
        with), and let `changed()` reload the engine after a save."""
        self.live = live

        def follow(lp: Any) -> None:
            self.config, self.registry = lp.config, lp.registry

        live.listeners.append(follow)

    def set_dir(self, root: str | Path) -> None:
        """Read and write another pipelines folder from now on (the running engine too)."""
        with self.lock:
            self.root = Path(root).resolve()
        if self.live is not None:
            self.live.set_dir(self.root)

    def changed(self, reason: str) -> None:
        """A pipeline file or a setting was written: the running engine picks it up now
        (a no-op without a live engine; its file watcher would also notice)."""
        if self.live is not None:
            self.live.check(reason)

    def resolve(self, name: str) -> Path:
        name = name.strip().removesuffix(".yaml")
        if not _NAME.match(name) or any(p in ("..", ".") for p in name.split("/")):
            raise HTTPException(400, f"bad pipeline name {name!r}")
        path = (self.root / f"{name}.yaml").resolve()
        if not path.is_relative_to(self.root):
            raise HTTPException(400, f"pipeline {name!r} is outside the pipelines directory")
        return path

    def name_of(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix().removesuffix(".yaml")

    def files(self) -> list[Path]:
        return [p for p in pipeline_files(self.root) if p.suffix == ".yaml"]

    def model(self, text: str, path: Path, *, validate: bool = True) -> dict[str, Any]:
        return to_model(
            text,
            path=path,
            base_dir=path.parent,
            registry=self.registry,
            config=self.config,
            validate=validate,
        )

    def find_by_pipeline_name(self, flow: str) -> tuple[str, dict[str, Any]] | None:
        """The file whose `name:` is `flow` (for drawing a run's graph)."""
        for p in self.files():
            try:
                m = self.model(_read(p), p, validate=False)
            except OSError:
                continue
            if m.get("name") == flow:
                return self.name_of(p), m
        return None


def create_editor_router(files: PipelineFiles) -> APIRouter:
    router = APIRouter()

    @router.get("/pipelines")
    def list_pipelines() -> dict[str, Any]:
        items = []
        for p in files.files():
            m = files.model(_read(p), p)
            items.append(
                {
                    "name": files.name_of(p),
                    "path": p.relative_to(files.root).as_posix(),
                    "pipeline": m.get("name"),
                    "nodes": len(m["nodes"]),
                    "issues": len(m["issues"]),
                }
            )
        return {"dir": str(files.root), "pipelines": items}

    @router.get("/pipelines/{name:path}")
    def get_pipeline(name: str) -> dict[str, Any]:
        path = files.resolve(name)
        if not path.is_file():
            raise HTTPException(404, f"no pipeline {name!r}")
        source = _read(path)
        return {
            "name": files.name_of(path),
            "path": path.relative_to(files.root).as_posix(),
            "source": source,
            "model": files.model(source, path),
        }

    @router.delete("/pipelines/{name:path}")
    def delete_pipeline(name: str) -> dict[str, Any]:
        """Delete a pipeline. One in its own folder (`<name>/pipeline.yaml` with no other
        pipeline beside it) goes with its folder (prompts, schemas, scripts); a loose `x.yaml`
        goes alone, since files next to it may be shared. Pipeline history keeps it."""
        path = files.resolve(name)
        if not path.is_file():
            raise HTTPException(404, f"no pipeline {name!r}")
        folder = path.parent
        others = [p for p in files.files() if p != path and p.is_relative_to(folder)]
        whole = folder != files.root and path.name == "pipeline.yaml" and not others
        label = files.name_of(path)
        with files.lock:
            history.record(files.root, "before delete")
            if whole:
                shutil.rmtree(folder)
            else:
                path.unlink()
            history.record(files.root, f"delete {label}")
        restart.note(f"pipeline {label} deleted")
        removed = folder.relative_to(files.root).as_posix() + "/" if whole else label + ".yaml"
        return {"deleted": label, "removed": removed}

    @router.put("/pipelines/{name:path}/source")
    def put_source(name: str, body: SourceBody) -> dict[str, Any]:
        path = files.resolve(name)
        with files.lock:
            history.record(files.root, "before edit")  # keeps the version before the first save
            _write(path, body.source)
            history.record(files.root, f"edit {files.name_of(path)} (YAML)")
        model = files.model(body.source, path)
        return {
            "name": files.name_of(path),
            "source": body.source,
            "model": model,
            "issues": model["issues"],
        }

    @router.put("/pipelines/{name:path}/model", response_model=None)
    def put_model(name: str, body: ModelBody) -> dict[str, Any] | JSONResponse:
        path = files.resolve(name)
        with files.lock:
            current = _read(path) if path.is_file() else ""
            if current != body.base_source:
                return JSONResponse(
                    {
                        "detail": "the file changed on disk since it was loaded; reload it",
                        "source": current,
                    },
                    status_code=409,
                )
            try:
                source, warnings = apply_model(current, body.model)
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
            if source != current:
                history.record(files.root, "before edit")
                _write(path, source)
                history.record(files.root, f"edit {files.name_of(path)}")
        model = files.model(source, path)
        return {"source": source, "model": model, "warnings": warnings, "issues": model["issues"]}

    @router.post("/validate")
    def validate(body: ValidateBody) -> dict[str, Any]:
        """Validate `source`; or, with `model`, apply it to `source` in memory (nothing is
        saved) and validate the result, which the editor uses as a live preview."""
        path = files.resolve(body.name) if body.name else files.root / "pipeline.yaml"
        source, warnings = body.source, []
        if body.model is not None:
            try:
                source, warnings = apply_model(body.source, body.model)
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
        model = files.model(source, path)
        return {"model": model, "issues": model["issues"], "source": source, "warnings": warnings}

    @router.get("/node-types")
    def get_node_types() -> dict[str, Any]:
        return node_types(files.registry, files.config)

    return router
