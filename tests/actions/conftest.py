from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from agentline import App, SqliteLedger
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.ledger.base import Effect
from agentline.pipeline import Pipeline, build_pipeline, parse_spec
from agentline.registry import Registry, RunRequest
from agentline.runners import FunctionRunner

from .fakes import GITHUB_TOKEN, JIRA_EMAIL, JIRA_TOKEN, FakeState, Repos, fake_server, make_repos


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, FakeState]]:
    with fake_server() as (url, state):
        monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)
        monkeypatch.setenv("GITHUB_API_URL", url)
        monkeypatch.setenv("JIRA_BASE_URL", url)
        monkeypatch.setenv("JIRA_EMAIL", JIRA_EMAIL)
        monkeypatch.setenv("JIRA_API_TOKEN", JIRA_TOKEN)
        yield url, state


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return make_repos(tmp_path)


Agent = Callable[[RunRequest], Awaitable[Any]]


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.ledger = SqliteLedger(tmp_path / "ledger.db")
        self.app = App(self.ledger, backoff_base=0.0)
        self.registry: Registry | None = None

    def pipeline(
        self, yaml: str, *, agent: Agent | None = None, config: Config | None = None
    ) -> Pipeline:
        reg = default_registry(config)
        if agent is not None:
            reg.add_runner(FunctionRunner("fn", agent))
        self.registry = reg
        p = build_pipeline(parse_spec(yaml, base_dir=self.tmp), reg, config)
        self.app.register_pipeline(p)
        return p

    def submit(self, name: str, payload: Any = None, dedupe: str | None = None) -> str:
        return self.app.submit_sync(name, payload or {}, dedupe).run_id

    def drain(self) -> None:
        asyncio.run(self.app.worker(concurrency=2, poll_interval=0.01).run_until_idle())

    def status(self, run_id: str) -> str:
        run = self.app.get_run(run_id)
        assert run is not None
        return run.status

    def statuses(self, run_id: str) -> dict[str, str]:
        return {x.key: x.status for x in self.app.executions(run_id)}

    def effects(self, run_id: str) -> dict[str, str]:
        with self.ledger.tx() as tx:
            return {
                f"{e.node_id}#{e.iteration}:{e.name}": e.status for e in tx.list_effects(run_id)
            }

    def leave_intent(self, run_id: str, node: str, name: str, iteration: int = 1) -> None:
        """Simulate a crash mid-effect: an intent with no outcome, as a dead worker leaves it."""
        with self.ledger.tx() as tx:
            tx.put_effect(Effect(run_id, node, iteration, name, status="intent", updated_at=1.0))

    def execution(self, run_id: str, key: str) -> Any:
        return next(x for x in self.app.executions(run_id) if x.key == key)


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    yield harness
    harness.ledger.close()
