"""Optional OpenTelemetry export (phase 58), against a fake OTLP/HTTP collector."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import feedback, telemetry
from agentline.api.server import create_server
from agentline.config import Config
from agentline.errors import ValidationError
from agentline.secrets import set_secret

from .fakes import _Server
from .test_review import World

FAKE_SECRET = "sk-fake-TELEMETRY-0123456789"
HEADER_VALUE = "Bearer fake-otlp-token-987654321"


class Collector:
    def __init__(self) -> None:
        self.got: list[tuple[str, dict[str, str], Any]] = []
        self.fail = False


@contextmanager
def collector() -> Iterator[tuple[str, Collector]]:
    st = Collector()

    class H(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
            if st.fail:
                self.send_response(503)
                self.end_headers()
                return
            st.got.append((self.path, dict(self.headers), body))
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = _Server(("127.0.0.1", 0), H)
    t = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", st
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_TELEMETRY", "on")


def conf(endpoint: str, **kw: Any) -> dict[str, Any]:
    return {**telemetry.DEFAULTS, "enabled": True, "endpoint": endpoint, **kw}


def test_config_defaults_and_checks(tmp_path: Path) -> None:
    assert telemetry.config(None) == telemetry.DEFAULTS
    assert telemetry.config(None)["enabled"] is False  # off by default
    path = tmp_path / "agentline.toml"
    path.write_text('[report]\ngithub_repo = "a/b"\n')
    with pytest.raises(ValidationError, match="address before"):
        telemetry.save(path, {"enabled": True})
    with pytest.raises(ValidationError, match="http"):
        telemetry.save(path, {"endpoint": "file:///etc/passwd"})
    with pytest.raises(ValidationError, match="not a secret name"):
        telemetry.save(path, {"headers_secret": "lower"})
    got = telemetry.save(path, {"enabled": True, "endpoint": "http://localhost:4318",
                                "headers_secret": "OTEL_HEADERS"})  # fmt: skip
    assert got["enabled"] and got["headers_secret"] == "OTEL_HEADERS"
    cfg = Config.load(path)
    assert telemetry.config(cfg)["endpoint"] == "http://localhost:4318"
    assert cfg.raw["report"]["github_repo"] == "a/b"  # the rest of the file is kept
    telemetry.save(path, {"enabled": False})
    assert telemetry.config(Config.load(path))["endpoint"] == "http://localhost:4318"


def test_killed_sends_nothing(tmp_path: Path) -> None:
    w = World(tmp_path)
    with collector() as (url, st):
        assert telemetry.killed()  # tests/conftest.py
        assert telemetry.test_send(w.app, conf(url))["ok"] is False

        async def go() -> None:
            stop = asyncio.Event()
            task = asyncio.create_task(
                telemetry.run_telemetry_loop(w.app, w.files, stop, interval=0.01)
            )
            await asyncio.sleep(0.1)
            stop.set()
            await task

        asyncio.run(go())
        assert st.got == []


def test_each_finished_run_is_sent_once_without_free_text(
    tmp_path: Path, on: None, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    w = World(tmp_path)
    w.app.redactor.add("FAKE_API_KEY", FAKE_SECRET)
    set_secret("OTEL_HEADERS", f"Authorization={HEADER_VALUE.replace(' ', '%20')}")
    with collector() as (url, st):
        c = conf(url, headers_secret="OTEL_HEADERS")
        assert telemetry.export_once(w.app, c)["sent"] == 0  # first call: starts from now
        run = w.run(loops=1)
        with w.app.ledger.tx() as tx:
            tx.update_run(run, error=f"boom {FAKE_SECRET}")
        feedback.add(w.app, run, "down", f"secret comment {FAKE_SECRET}")
        res = telemetry.export_once(w.app, c)
        assert res == {"sent": 1, "failed": 0, "queued": 0}
        assert [p for p, _, _ in st.got] == ["/v1/traces", "/v1/metrics"]
        assert st.got[0][1]["Authorization"] == HEADER_VALUE
        traces = st.got[0][2]
        spans = traces["resourceSpans"][0]["scopeSpans"][0]["spans"]
        with w.app.ledger.tx() as tx:
            n_execs = len(tx.list_executions(run))
        assert spans[0]["name"] == "run p" and len(spans) == 1 + n_execs  # run + executions
        assert all(s["traceId"] == spans[0]["traceId"] for s in spans)
        attrs = {a["key"]: a["value"] for a in spans[0]["attributes"]}
        assert attrs["agentline.run.id"]["stringValue"] == run
        assert attrs["agentline.feedback.down"]["intValue"] == "1"
        names = [
            m["name"] for m in st.got[1][2]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        ]
        assert "agentline.runs" in names and "agentline.cost_usd" in names
        everything = json.dumps([b for _, _, b in st.got])
        for leak in (FAKE_SECRET, "secret comment", "boom", "a draft", "T-1", HEADER_VALUE):
            assert leak not in everything, leak
        assert telemetry.export_once(w.app, c)["sent"] == 0  # once only
        assert len(st.got) == 2
        # the collector is down: the next run waits, then goes once it answers
        run2 = w.run(loops=0)
        st.fail = True
        with caplog.at_level(logging.WARNING):
            res = telemetry.export_once(w.app, c)
        assert res["failed"] == 1 and "503" in res["error"] and res["queued"] == 1
        assert HEADER_VALUE not in json.dumps(res)
        st.fail = False
        assert telemetry.export_once(w.app, c)["sent"] == 1
        sent_ids = {
            a["value"]["stringValue"]
            for _, _, b in st.got
            if "resourceSpans" in b
            for a in b["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
            if a["key"] == "agentline.run.id"
        }
        assert sent_ids == {run, run2}
        assert telemetry.read_state()["last"]["ok"] is True


def test_api_and_test_span(tmp_path: Path, on: None) -> None:
    w = World(tmp_path)
    set_secret("OTEL_HEADERS", f"x-key={HEADER_VALUE}")
    c = TestClient(create_server(w.app, [], pipelines_dir=w.pipes, config=w.cfg))
    with collector() as (url, st):
        r = c.put("/api/telemetry", json={"enabled": True, "endpoint": url,
                                         "headers_secret": "OTEL_HEADERS"})  # fmt: skip
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] and body["headers_set"] and HEADER_VALUE not in r.text
        assert "enabled = true" in (tmp_path / "agentline.toml").read_text()
        assert c.put("/api/telemetry", json={"endpoint": "nope"}).status_code == 400
        t = c.post("/api/telemetry/test").json()
        assert t["ok"] is True and st.got[0][2]["resourceSpans"][0]["scopeSpans"][0]["spans"][0][
            "name"] == "agentline.test"  # fmt: skip
        assert c.get("/api/telemetry").json()["endpoint"] == url
