"""A fake OpenAI-compatible server (chat completions + model list), like OpenRouter or a
private LLM server. Replies are scripted per request."""

from __future__ import annotations

import json
import socketserver
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KEY = "sk-or-fake-0123456789abcdef"


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:  # skip socket.getfqdn() (slow on macOS)
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)


@dataclass
class FakeLLM:
    replies: list[Any] = field(default_factory=list)  # str content, or (status, body) tuples
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    require_key: str | None = KEY
    usage_cost: float | None = None
    reply: Callable[[dict[str, Any]], str] | None = None


@contextmanager
def fake_llm(state: FakeLLM | None = None) -> Iterator[tuple[str, FakeLLM]]:
    st = state or FakeLLM()

    class H(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def send(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authed(self) -> bool:
            if st.require_key and self.headers.get("authorization") != f"Bearer {st.require_key}":
                self.send(401, {"error": {"message": "No auth credentials found"}})
                return False
            return True

        def do_GET(self) -> None:
            if not self.authed():
                return
            if self.path.rstrip("/").endswith("/models"):
                return self.send(
                    200,
                    {
                        "data": [
                            {
                                "id": "openai/gpt-big",
                                "name": "Big",
                                "pricing": {"prompt": "0.000002", "completion": "0.000008"},
                            },
                            {
                                "id": "meta-llama/llama-3.3-70b-instruct:free",
                                "name": "Llama free",
                                "context_length": 131072,
                            },
                            {"id": "qwen/qwen3-8b", "pricing": {"prompt": "0", "completion": "0"}},
                        ]
                    },
                )
            self.send(404, {"error": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            st.headers.append(dict(self.headers))
            if not self.authed():
                return
            st.requests.append(body)
            if st.reply is not None:
                content: Any = st.reply(body)
            elif st.replies:
                content = st.replies.pop(0)
            else:
                content = "ok"
            if isinstance(content, tuple):
                return self.send(content[0], content[1])
            usage: dict[str, Any] = {
                "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500
            }  # fmt: skip
            if st.usage_cost is not None:
                usage["cost"] = st.usage_cost
            # a dict is the whole assistant message (tool calls: see `tool_calls`)
            message = {"role": "assistant", "content": content}
            finish = "stop"
            if isinstance(content, dict):
                content = dict(content)
                given = content.pop("finish_reason", None)  # e.g. OpenRouter's "error"
                message = {"role": "assistant", "content": None, **content}
                finish = given or ("tool_calls" if content.get("tool_calls") else "stop")
            self.send(
                200,
                {
                    "id": "gen-1",
                    "object": "chat.completion",
                    "created": 1760000000,
                    "model": body.get("model"),
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": usage,
                },
            )

    server = _Server(("127.0.0.1", 0), H)
    t = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/api/v1", st
    finally:
        server.shutdown()
        server.server_close()


def tool_calls(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """An assistant message calling tools (OpenAI format), for FakeLLM replies."""
    return {
        "tool_calls": [
            {"id": f"call_{i}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(calls)
        ]
    }  # fmt: skip
