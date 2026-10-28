"""Live reload: the engine picks up settings and pipeline changes without a restart (phase 42).

`LivePipelines` owns what the engine runs: the config, the registry built from it, and every
pipeline compiled from the pipelines folder. `reload()` reads them again and swaps the graphs
into the App (`App.replace_graphs`):
- a settings change (a model, a source, a key name, a harness) recompiles every pipeline; a
  graph whose structure is unchanged replaces the old one, so runs already going use the new
  settings for their remaining steps and retries;
- a pipeline whose nodes or edges changed applies to new runs; a run keeps the structure it
  started with (its `flow_hash`, see `App.graph`);
- a pipeline that no longer validates keeps its last good version (and says so);
- a deleted pipeline takes no new runs; runs already going finish.
Triggers read `pipelines` here each time (schedules, Jira polling, webhooks), so they follow.

What starts a reload: the API after a write (`check()` right away), and `watch()`, which looks
at the files' modification times every second or so (hand edits, the CLI, another process).
Keys need no recompile: runners read the key's env var when they run. Saving a key through the
API sets it at once; a change to the secrets file from outside (the Mac app's Keys screen, a
hand edit) is synced into the environment by `check()` (secrets.sync_secrets).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentline.config import Config
from agentline.errors import ValidationError
from agentline.pipeline import Pipeline, load_pipeline
from agentline.pipeline.files import pipeline_files
from agentline.registry import Registry
from agentline.secrets import read_secrets, secrets_path, sync_secrets

log = logging.getLogger("agentline.live")
WATCH_EVERY = 1.0  # seconds between file checks


def _default_registry(cfg: Config) -> Registry:
    from agentline.builtins import default_registry

    return default_registry(cfg)


class LivePipelines:
    def __init__(
        self,
        app: Any,
        pipelines_dir: str | Path,
        config_path: str | Path | None,
        *,
        registry_factory: Callable[[Config], Registry] = _default_registry,
        config: Config | None = None,
        registry: Registry | None = None,
        pipelines: list[Pipeline] | None = None,
    ) -> None:
        """`config` / `registry` / `pipelines` are what is loaded now (the start-up load); when
        given, they are taken as is and the first reload happens on the first change."""
        self.app = app
        self.dir = Path(pipelines_dir)
        self.config_path = Path(config_path) if config_path else None
        self.registry_factory = registry_factory
        self.config = config if config is not None else self._load_config()
        self.registry = registry if registry is not None else registry_factory(self.config)
        self.pipelines: list[Pipeline] = list(pipelines or [])
        self.version = 0  # bumped by every reload that changed something
        self.last: dict[str, Any] = {}  # the last reload's summary (GET /api/status)
        self.listeners: list[Callable[[LivePipelines], None]] = []
        self._lock = threading.Lock()
        self._secrets = read_secrets()
        self._secrets_stamp = self._secrets_mtime()
        self._by_file: dict[Path, Pipeline] = {}
        self._stamp = self._fingerprint()
        if pipelines is None:
            self.reload("start")
        else:
            self._by_file = {Path(p.spec.path).resolve(): p for p in self.pipelines if p.spec.path}

    def __iter__(self) -> Any:  # the current pipelines, like the plain list it replaces
        return iter(list(self.pipelines))

    def __len__(self) -> int:
        return len(self.pipelines)

    # -- what changed on disk ---------------------------------------------------------------
    def _load_config(self) -> Config:
        if self.config_path is not None and self.config_path.exists():
            return Config.load(self.config_path)
        return Config(path=self.config_path)

    def _files(self) -> list[Path]:
        return pipeline_files(self.dir) if self.dir.is_dir() else []

    def _fingerprint(self) -> tuple[Any, ...]:
        """Modification times of the config and every pipeline folder's files."""
        stamps: list[tuple[str, int]] = []
        if self.config_path is not None and self.config_path.exists():
            stamps.append((str(self.config_path), self.config_path.stat().st_mtime_ns))
        for f in self._files():
            folder = f.parent if f.name in ("pipeline.yaml", "pipeline.yml") else None
            paths = [f] if folder is None else [x for x in folder.rglob("*") if x.is_file()]
            for x in paths:
                with contextlib.suppress(OSError):
                    stamps.append((str(x), x.stat().st_mtime_ns))
        return tuple(sorted(stamps))

    def _secrets_mtime(self) -> int | None:
        with contextlib.suppress(OSError):
            return secrets_path().stat().st_mtime_ns
        return None

    def check(self, reason: str = "files changed") -> dict[str, Any] | None:
        """Reload when the config or a pipeline file changed since the last load; follow the
        secrets file when it changed."""
        stamp = self._secrets_mtime()
        if stamp != self._secrets_stamp:
            self._secrets_stamp = stamp
            self._secrets = sync_secrets(self._secrets)
            log.info("live reload: the secrets file changed; keys updated")
        if self._fingerprint() == self._stamp:
            return None
        return self.reload(reason)

    # -- reload -----------------------------------------------------------------------------
    def reload(self, reason: str = "reload") -> dict[str, Any]:
        """Read the config and every pipeline again and swap them in. Never raises: a broken
        config keeps the old one, a broken pipeline keeps its last good version."""
        with self._lock:
            self._stamp = self._fingerprint()
            problems: dict[str, str] = {}
            try:
                cfg = self._load_config()
                registry = self.registry_factory(cfg)
            except (ValidationError, OSError, ValueError) as e:
                problems["agentline.toml"] = f"kept the previous settings: {e}"
                cfg, registry = self.config, self.registry
            compiled: dict[Path, Pipeline] = {}
            for f in self._files():
                key = f.resolve()
                try:
                    compiled[key] = load_pipeline(f, registry, cfg)
                except (ValidationError, OSError, ValueError) as e:
                    old = self._by_file.get(key)
                    note = "kept the last good version" if old else "not loaded"
                    problems[f.relative_to(self.dir).as_posix()] = f"{note}: {_first_line(e)}"
                    if old is not None:
                        compiled[key] = old
            graphs: dict[str, Any] = {}
            for p in compiled.values():
                if p.name in graphs:
                    problems[p.name] = "two files define this pipeline name; the first one wins"
                    continue
                graphs[p.name] = p.graph
            changes = self.app.replace_graphs(graphs)
            self.config, self.registry = cfg, registry
            self._by_file = compiled
            self.pipelines = [p for p in compiled.values() if graphs.get(p.name) is p.graph]
            self.version += 1
            self.last = {"reason": reason, "at": time.time(), **changes, "problems": problems}
        if "agentline.toml" not in problems:  # the engine runs what was saved: nothing to restart
            from agentline import restart

            restart.clear()
        for name, msg in problems.items():
            log.warning("live reload (%s): %s: %s", reason, name, msg)
        if reason != "start":
            log.info(
                "live reload (%s): %d pipeline(s); added %s, restructured %s, removed %s",
                reason, len(self.pipelines), changes["added"] or "-",
                changes["restructured"] or "-", changes["removed"] or "-",
            )  # fmt: skip
        for fn in list(self.listeners):
            with contextlib.suppress(Exception):
                fn(self)
        return self.last

    async def watch(self, stop: asyncio.Event, every: float = WATCH_EVERY) -> None:
        """Reload whenever the files change, until `stop` is set."""
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=every)
            if stop.is_set():
                return
            try:
                await asyncio.to_thread(self.check)
            except Exception:  # keep watching; the next change tries again
                log.exception("live reload failed")


def current(pipelines: Any) -> list[Any]:
    """The pipelines to use now: a `LivePipelines`' current list, or a plain list as is."""
    return list(pipelines.pipelines) if isinstance(pipelines, LivePipelines) else list(pipelines)


def version(pipelines: Any) -> int:
    return pipelines.version if isinstance(pipelines, LivePipelines) else 0


def _first_line(e: BaseException) -> str:
    return str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__
