"""anthropic runner against a local fake Messages API (no network)."""

from __future__ import annotations

import asyncio
import json
import socketserver
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agentline.config import Config
from agentline.errors import BudgetExceeded, PermanentError
from agentline.registry import Limits, Registry, RunRequest, RunResult
from agentline.runners import AnthropicRunner, RunnerError, register


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:  # skip getfqdn (slow on macOS)
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)


class FakeAPI:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.text = '```json\n{"answer": 42}\n```'

    def handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        **body,
                    }
                )
                if api.status != 200:
                    data = json.dumps({"type": "error", "error": {"message": "nope"}}).encode()
                else:
                    data = json.dumps(
                        {
                            "id": "msg_1",
                            "type": "message",
                            "model": body["model"],
                            "content": [{"type": "text", "text": api.text}],
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 1000, "output_tokens": 2000},
                        }
                    ).encode()
                self.send_response(api.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return H


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAPI]:
    fake = FakeAPI()
    server = _Server(("127.0.0.1", 0), fake.handler())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    yield fake
    server.shutdown()
    server.server_close()


def call(runner: AnthropicRunner, params: dict[str, Any], **kw: Any) -> RunResult:
    req = RunRequest("What is it?", {}, kw.pop("limits", Limits()), params=params)
    return asyncio.run(runner.run(req))


def test_json_output_and_cost_from_params(api: FakeAPI) -> None:
    res = call(
        AnthropicRunner(),
        {"input_usd_per_mtok": 3.0, "output_usd_per_mtok": 15.0, "system": "be terse"},
    )
    assert res.output == {"answer": 42}
    assert res.cost_usd == pytest.approx((1000 * 3 + 2000 * 15) / 1e6)
    (r,) = api.requests
    assert r["path"] == "/v1/messages" and r["model"] == "claude-sonnet-5-5"
    assert r["headers"]["x-api-key"] == "sk-test-not-real"
    assert r["headers"]["anthropic-version"] == "2023-06-01"
    assert r["messages"] == [{"role": "user", "content": "What is it?"}]
    assert r["system"] == "be terse" and r["max_tokens"] == 4096
    assert "sk-test-not-real" not in res.transcript
    assert res.meta["usage"]["output_tokens"] == 2000


def test_cost_from_config_pricing_table(api: FakeAPI) -> None:
    reg = Registry()
    register(reg, Config.from_dict({"pricing": {"m1": {"input": 1.0, "output": 2.0}}}))
    runner = reg.get_runner("anthropic")
    assert isinstance(runner, AnthropicRunner)
    api.text = "plain words"
    res = call(runner, {"model": "m1"})
    assert res.output == "plain words"
    assert res.cost_usd == pytest.approx((1000 * 1 + 2000 * 2) / 1e6)


def test_unknown_price_records_zero_with_warning(api: FakeAPI) -> None:
    res = call(AnthropicRunner(), {})
    assert res.cost_usd == 0.0
    assert "no price known" in res.meta["warnings"][0]


def test_zero_budget_refuses_without_calling(api: FakeAPI) -> None:
    with pytest.raises(BudgetExceeded):
        call(AnthropicRunner(), {}, limits=Limits(budget_usd=0.0))
    assert api.requests == []


def test_http_errors(api: FakeAPI) -> None:
    api.status = 529
    with pytest.raises(RunnerError, match="529"):
        call(AnthropicRunner(), {})
    api.status = 400
    with pytest.raises(PermanentError, match="400"):
        call(AnthropicRunner(), {})


def test_missing_key(api: FakeAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(PermanentError, match="ANTHROPIC_API_KEY"):
        call(AnthropicRunner(), {})
