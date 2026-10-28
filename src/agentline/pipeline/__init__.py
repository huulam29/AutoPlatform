"""YAML pipelines: parse -> validate -> compile to a Graph the engine runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentline.config import Config
from agentline.graph import Graph
from agentline.pipeline.compile import compile_pipeline
from agentline.pipeline.spec import Issue, PipelineSpec, SpecError, load_spec, parse_spec
from agentline.pipeline.validate import check_spec, validate_spec
from agentline.registry import Registry


@dataclass
class Pipeline:
    spec: PipelineSpec
    graph: Graph

    @property
    def name(self) -> str:
        return self.spec.name


def build_pipeline(
    spec: PipelineSpec, registry: Registry, config: Config | None = None
) -> Pipeline:
    check_spec(spec, registry, config)
    return Pipeline(spec, compile_pipeline(spec, registry, config))


def load_pipeline(path: str | Path, registry: Registry, config: Config | None = None) -> Pipeline:
    return build_pipeline(load_spec(path), registry, config)


__all__ = [
    "Issue",
    "Pipeline",
    "PipelineSpec",
    "SpecError",
    "build_pipeline",
    "check_spec",
    "compile_pipeline",
    "load_pipeline",
    "load_spec",
    "parse_spec",
    "validate_spec",
]
