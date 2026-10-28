"""Shared plumbing for websocket chat platforms (Slack Socket Mode, Discord Gateway): a reader
thread keeps the connection open and turns platform events into Telegram-shaped updates
(`{"update_id", "message": {...}}` / `{"update_id", "callback_query": {...}}`), so the
chat-link loop (service.py) handles every platform the same way."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from agentline.chatlink.ws import WebSocket, WebSocketClosed

log = logging.getLogger("agentline.chatlink")


class LinkError(ConnectionError):
    pass


def http_json(
    method: str, url: str, headers: dict[str, str], body: Any = None, timeout: float = 20
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    h = {"content-type": "application/json; charset=utf-8", "user-agent": "agentline", **headers}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        raise LinkError(f"{method} {url.split('?')[0]}: HTTP {e.code} {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LinkError(
            f"{method} {url.split('?')[0]}: can't connect ({type(e).__name__})"
        ) from None
    return json.loads(raw) if raw else None


class SocketTransport:
    """Subclasses implement `connect() -> WebSocket` and `handle(ws, message)` (which calls
    `self.push(update)`), plus send / answer / clear_buttons / me."""

    reconnect_on_error = True

    def __init__(self, listen: bool = True) -> None:
        """`listen=False`: only send (API test messages, chat.send steps), no connection."""
        self._q: queue.Queue[dict[str, Any]] = queue.Queue()
        self._seq = 0
        self._stop = threading.Event()
        self._ws: WebSocket | None = None
        self.error: str | None = None
        if listen:
            self._thread = threading.Thread(target=self._run, name=type(self).__name__, daemon=True)
            self._thread.start()

    # -- the reader thread ----------------------------------------------------------------------
    def connect(self) -> WebSocket:  # pragma: no cover - per platform
        raise NotImplementedError

    def handle(self, ws: WebSocket, message: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                ws = self._ws = self.connect()
                self.error = None
                delay = 1.0
                while not self._stop.is_set():
                    self.handle(ws, ws.recv_json())
            except (WebSocketClosed, OSError, ValueError, LinkError) as e:
                self.error = str(e)
                if not self._stop.is_set():
                    log.warning("%s: %s (reconnecting in %.0fs)", type(self).__name__, e, delay)
                    self._stop.wait(delay)
                    delay = min(60.0, delay * 2)
            finally:
                if self._ws is not None:
                    self._ws.close()
                    self._ws = None

    def push(self, update: dict[str, Any]) -> None:
        self._seq += 1
        self._q.put({"update_id": self._seq, **update})

    # -- what the chat-link loop calls ----------------------------------------------------------
    def updates(self, offset: int | None, wait: int = 5) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            out.append(self._q.get(timeout=max(0.05, float(wait))))
            while True:
                out.append(self._q.get_nowait())
        except queue.Empty:
            pass
        return out

    def close(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            ws.close()

    def wait_ready(self, timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        while self._ws is None and time.monotonic() < end and self.error is None:
            time.sleep(0.05)


def message_update(
    msg_id: str, chat: str, user: str, name: str, text: str, private: bool, reply_to: str | None
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "message_id": msg_id,
        "chat": {"id": chat, "type": "private" if private else "group"},
        "from": {"id": user, "username": name},
        "text": text,
    }
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"message": m}


def tap_update(tap_id: str, chat: str, user: str, data: str, msg_id: str) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": tap_id,
            "from": {"id": user},
            "data": data,
            "message": {"message_id": msg_id, "chat": {"id": chat}},
        }
    }
