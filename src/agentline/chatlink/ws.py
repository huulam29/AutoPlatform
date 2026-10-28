"""A small websocket client (RFC 6455) on the standard library, for Slack Socket Mode and the
Discord Gateway: text frames, ping/pong, close, fragmented messages. Client frames are masked.
No compression extensions are negotiated.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import urllib.parse
from typing import Any

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise ValueError(f"not a websocket url: {u.scheme}://")
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=u.hostname)
        self.sock = raw
        self._send_lock = threading.Lock()
        self._buf = b""
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        head = self._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ConnectionError(f"websocket handshake failed: {status.decode(errors='replace')}")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if accept.encode() not in head:
            raise ConnectionError("websocket handshake: bad Sec-WebSocket-Accept")

    # -- reading -------------------------------------------------------------------------------
    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketClosed("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketClosed("connection closed during handshake")
            self._buf += chunk
        i = self._buf.index(marker) + len(marker)
        out, self._buf = self._buf[:i], self._buf[i:]
        return out

    def _frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._read(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read(8))
        mask = self._read(4) if b2 & 0x80 else None
        data = self._read(length)
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return fin, opcode, data

    def recv(self) -> str:
        """The next text (or binary, decoded) message; answers pings; raises on close."""
        parts: list[bytes] = []
        while True:
            fin, opcode, data = self._frame()
            if opcode == 0x9:  # ping
                self._send(0xA, data)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005
                raise WebSocketClosed(f"closed by the server ({code})")
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    def recv_json(self) -> Any:
        return json.loads(self.recv())

    # -- writing -------------------------------------------------------------------------------
    def _send(self, opcode: int, data: bytes) -> None:
        head = bytes([0x80 | opcode])
        n = len(data)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        with self._send_lock:
            self.sock.sendall(head + mask + masked)

    def send(self, text: str) -> None:
        self._send(0x1, text.encode())

    def send_json(self, value: Any) -> None:
        self.send(json.dumps(value))

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._send(0x8, struct.pack("!H", 1000))
        with contextlib.suppress(OSError):
            self.sock.close()
