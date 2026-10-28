"""Tiny JSON-over-HTTP helper on stdlib urllib, with secret redaction in every error."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from agentline.errors import AgentlineError, PermanentError

USER_AGENT = "agentline/0.1"
_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class ApiError(AgentlineError):
    """An HTTP call failed. Retryable (5xx, 429, network); see PermanentApiError for 4xx."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class PermanentApiError(ApiError, PermanentError):
    """4xx other than 408/429: retrying the same request won't help."""


class MissingCredentials(PermanentError):
    """A required env var (token, base URL) is not set."""


def redact(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    return text


@dataclass
class Response:
    status: int
    data: Any
    headers: dict[str, str]

    def next_link(self) -> str | None:
        m = _NEXT_LINK.search(self.headers.get("link", ""))
        return m.group(1) if m else None


class JsonHttp:
    """Base for API clients: one `request()` that raises redacted ApiErrors."""

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        secrets: Iterable[str],
        *,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
        self._secrets = tuple(s for s in secrets if s)
        self.timeout = timeout

    def _redact(self, text: str) -> str:
        return redact(text, self._secrets)

    def url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        expect: tuple[int, ...] = (200, 201, 204),
    ) -> Response:
        url = self.url(path)
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self._headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            text = self._redact(e.read().decode("utf-8", "replace")[:500])
            msg = self._redact(f"{method} {url} -> HTTP {e.code}: {text}")
            cls = ApiError if e.code >= 500 or e.code in (408, 429) else PermanentApiError
            raise cls(msg, e.code, text) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise ApiError(self._redact(f"{method} {url} failed: {reason}")) from None
        if status not in expect:
            raise ApiError(self._redact(f"{method} {url} -> unexpected HTTP {status}"), status)
        parsed = json.loads(raw) if raw.strip() else None
        return Response(status, parsed, resp_headers)

    def get_all(self, path: str, *, max_pages: int = 50) -> list[Any]:
        """GET a list endpoint, following `Link: rel="next"` pagination."""
        out: list[Any] = []
        next_url: str | None = path
        for _ in range(max_pages):
            if next_url is None:
                break
            resp = self.request("GET", next_url)
            out.extend(resp.data or [])
            next_url = resp.next_link()
        return out
