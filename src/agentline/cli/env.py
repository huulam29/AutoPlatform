"""What every CLI command needs: paths, config, registry, pipelines, the App, and output that
is redacted before it is printed."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from functools import cached_property
from pathlib import Path
from typing import IO, Any

from agentline.app import App, default_actor
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.errors import ValidationError
from agentline.ledger import is_postgres_url, ledger_from_url, sqlite_path
from agentline.pipeline import Pipeline, SpecError, load_pipeline
from agentline.redact import Redactor
from agentline.registry import Registry

REGISTRY_ENV = "AGENTLINE_REGISTRY"


def agentline_home() -> Path:
    return Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline").expanduser()


class CliEnv:
    def __init__(
        self,
        args: argparse.Namespace,
        stdout: IO[str] | None = None,
        stderr: IO[str] | None = None,
    ) -> None:
        self.args = args
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.redactor = Redactor.from_env()
        self._app: App | None = None

    # -- output -----------------------------------------------------------------------------
    def out(self, text: str = "") -> None:
        print(self.redactor.redact(text), file=self.stdout)

    def err(self, text: str) -> None:
        print(self.redactor.redact(text), file=self.stderr)

    def out_json(self, value: Any) -> None:
        self.out(json.dumps(self.redactor.redact_value(value), indent=2, default=str))

    # -- settings ---------------------------------------------------------------------------
    @property
    def actor(self) -> str:
        return default_actor()

    @cached_property
    def db_url(self) -> str | Path:
        """`--db` / $AGENTLINE_DB: a path, `sqlite:///path` or `postgresql://...`."""
        raw = getattr(self.args, "db", None) or os.environ.get("AGENTLINE_DB")
        if raw and is_postgres_url(raw):
            return str(raw)
        path = sqlite_path(raw) if raw else agentline_home() / "agentline.db"
        if path is None:
            raise ValidationError(f"--db: unsupported ledger URL {raw!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def db_path(self) -> Path | str:
        """The ledger for display: the SQLite file, or the Postgres URL without its password."""
        if isinstance(self.db_url, Path):
            return self.db_url
        from agentline.ledger.postgres import redact_dsn

        return redact_dsn(self.db_url)

    @cached_property
    def config(self) -> Config:
        cfg = Config.load(getattr(self.args, "config", None))
        # once per data folder: keep the harnesses this install relied on (phase 44)
        from agentline.harness_migration import marker_path, migrate

        if not marker_path().exists():
            raw = getattr(self.args, "config", None) or os.environ.get("AGENTLINE_CONFIG")
            path = cfg.path or Path(raw or "agentline.toml")
            if migrate(path, self._pipelines_dir_for(cfg))["harnesses"] and path.exists():
                cfg = Config.load(path)
        return cfg

    @property
    def config_path(self) -> Path:
        """Where agentline.toml is (or would be created): the loaded file, else --config,
        $AGENTLINE_CONFIG, ./agentline.toml."""
        if self.config.path is not None:
            return self.config.path
        raw = getattr(self.args, "config", None) or os.environ.get("AGENTLINE_CONFIG")
        return Path(raw or "agentline.toml")

    @cached_property
    def registry(self) -> Registry:
        return self.registry_for(self.config)

    def registry_for(self, config: Config) -> Registry:
        """`--registry module:function` / $AGENTLINE_REGISTRY (a function `(config) ->
        Registry`, e.g. test fakes), else the built-in default registry. Live reloads
        (`live`) build theirs with this too."""
        spec = getattr(self.args, "registry", None) or os.environ.get(REGISTRY_ENV)
        if not spec:
            return default_registry(config)
        mod_name, _, fn_name = spec.partition(":")
        if not fn_name:
            raise ValidationError(f"registry must be module:function, not {spec!r}")
        fn = getattr(importlib.import_module(mod_name), fn_name)
        reg = fn(config)
        if not isinstance(reg, Registry):
            raise ValidationError(f"{spec} returned {type(reg).__name__}, not a Registry")
        return reg

    @property
    def pipelines_dir(self) -> Path:
        return self._pipelines_dir_for(self.config)

    def _pipelines_dir_for(self, config: Config) -> Path:
        raw = getattr(self.args, "pipelines", None)
        if raw:
            return Path(raw).expanduser()
        cfg = config.raw.get("pipelines_dir")
        if cfg:
            p = Path(str(cfg)).expanduser()
            if not p.is_absolute() and config.path is not None:
                p = config.path.parent / p
            return p
        return Path("pipelines")

    @cached_property
    def pipelines(self) -> list[Pipeline]:
        """Every `*.yaml` / `*.yml` in the pipelines dir. Invalid ones are reported and skipped
        (`agentline validate` shows the details)."""
        out: list[Pipeline] = []
        d = self.pipelines_dir
        if not d.is_dir():
            return out
        from agentline.pipeline.files import pipeline_files

        for f in pipeline_files(d):  # top level and `<name>/pipeline.yaml`
            try:
                out.append(load_pipeline(f, self.registry, self.config))
            except SpecError as e:
                self.err(f"warning: skipping invalid pipeline {f} ({len(e.issues)} issue(s))")
        return out

    @property
    def app(self) -> App:
        if self._app is None:
            from agentline.incidents import IncidentStore

            incidents = IncidentStore(redactor=self.redactor)
            app = App(ledger_from_url(self.db_url), redactor=self.redactor, incidents=incidents)
            for p in self.pipelines:
                app.register_pipeline(p)
            self._app = app
        return self._app

    @cached_property
    def live(self) -> Any:
        """The pipelines the long-running commands (serve, worker) run, reloaded in place when
        settings or pipeline files change (agentline.live)."""
        from agentline.live import LivePipelines

        return LivePipelines(
            self.app,
            self.pipelines_dir,
            self.config_path,
            registry_factory=self.registry_for,
            config=self.config,
            registry=self.registry,
            pipelines=self.pipelines,
        )

    def load_pipeline_file(self, path: str | Path) -> Pipeline:
        p = load_pipeline(path, self.registry, self.config)
        self.app.register_pipeline(p)
        return p

    def close(self) -> None:
        if self._app is not None:
            self._app.ledger.close()
