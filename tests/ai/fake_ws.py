"""A tiny websocket server for tests: sends scripted JSON messages, records what it receives."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
from typing import Any

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeWS:
    def __init__(self, script: list[Any], ping_first: bool = False) -> None:
        self.script = list(script)
        self.ping_first = ping_first
        self.received: list[Any] = []
        self.got_pong = threading.Event()
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/socket"
        threading.Thread(target=self._serve, daemon=True).start()

    def _frame(self, opcode: int, data: bytes) -> bytes:
        n = len(data)
        head = bytes([0x80 | opcode])
        head += bytes([n]) if n < 126 else bytes([126]) + struct.pack("!H", n)
        return head + data

    def _read_frame(self, conn: socket.socket) -> tuple[int, bytes]:
        def need(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = conn.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError
                buf += chunk
            return buf

        b1, b2 = need(2)
        n = b2 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", need(2))
        mask = need(4)
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(need(n)))
        return b1 & 0x0F, data

    def _serve(self) -> None:
        conn, _ = self.srv.accept()
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        key = next(
            line.split(b":", 1)[1].strip().decode()
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key")
        )
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        conn.sendall(
            f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        if self.ping_first:
            conn.sendall(self._frame(0x9, b"hi"))
        for msg in self.script:
            data = json.dumps(msg).encode()
            # split the first message in two frames to exercise continuation frames
            if msg is self.script[0] and len(data) > 4:
                conn.sendall(bytes([0x01, len(data[:4])]) + data[:4])
                conn.sendall(self._frame(0x0, data[4:]))
            else:
                conn.sendall(self._frame(0x1, data))
        try:
            while True:
                op, data = self._read_frame(conn)
                if op == 0xA:
                    self.got_pong.set()
                elif op == 0x1:
                    self.received.append(json.loads(data))
                elif op == 0x8:
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()
