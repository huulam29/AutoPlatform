"""agentline serve: the ASGI app factory, the missing-extra error, and clean shutdown."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from agentline.cli import commands
from tests.cli.helpers import Cli


def test_build_asgi_app_serves_webhooks(cli: Cli) -> None:
    from fastapi.testclient import TestClient

    from agentline.cli import build_parser
    from agentline.cli.env import CliEnv

    args = build_parser().parse_args(
        ["--config", str(cli.config), "--pipelines", str(cli.pipelines), "serve"]
    )
    env = CliEnv(args)
    try:
        asgi = commands._build_asgi_app(env.app, env.pipelines, env.registry, env)
        with TestClient(asgi) as client:
            assert client.get("/healthz").json() == {"status": "ok"}
            assert client.post("/webhooks/github", content=b"{}").status_code == 401
    finally:
        env.close()


def test_serve_without_server_extra(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    code, _, err = cli.run("serve")
    assert code == 1 and "pip install 'agentline[server]'" in err


def test_serve_runs_worker_and_stops_with_the_server(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uvicorn isn't a dev dependency, so a stand-in module records what serve() asks for."""
    seen: dict[str, Any] = {}

    class Config:
        def __init__(self, app: Any, **kw: Any) -> None:
            self.app, self.kw = app, kw

    class Server:
        def __init__(self, config: Config) -> None:
            self.config = config
            self.should_exit = False

        async def serve(self) -> None:
            seen.update(self.config.kw)
            seen["app"] = type(self.config.app).__name__

    fake = types.ModuleType("uvicorn")
    fake.Config, fake.Server = Config, Server  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    cli.ok("serve", "--port", "18999")  # returns once the server returns: worker stopped too
    assert seen == {
        "host": "127.0.0.1",
        "port": 18999,
        "log_config": None,
        "lifespan": "off",
        "app": "FastAPI",
    }
