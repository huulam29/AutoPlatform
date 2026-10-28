"""Jira Cloud REST v3 client (stdlib urllib). Credentials from env vars only:
JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN."""

from __future__ import annotations

import base64
import os
import urllib.parse
from collections.abc import Mapping
from typing import Any

from agentline.integrations.http import JsonHttp, MissingCredentials, PermanentApiError


def adf_to_text(node: Any) -> str:
    """Flatten Atlassian Document Format (or a plain string) to text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return str(node)
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji"):
        attrs = node.get("attrs") or {}
        return str(attrs.get("text") or attrs.get("shortName") or "")
    inner = adf_to_text(node.get("content") or [])
    if kind == "listItem":
        return f"- {inner.strip()}\n"
    if kind in ("paragraph", "heading", "codeBlock", "blockquote", "rule"):
        return inner.rstrip("\n") + "\n\n"
    return inner


def text_to_adf(text: str) -> dict[str, Any]:
    """Plain text -> ADF doc: blank lines separate paragraphs, newlines become hard breaks."""
    paragraphs = []
    for para in text.split("\n\n"):
        content: list[dict[str, Any]] = []
        for i, line in enumerate(para.split("\n")):
            if i:
                content.append({"type": "hardBreak"})
            if line:
                content.append({"type": "text", "text": line})
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _user(u: Any) -> dict[str, Any] | None:
    if not isinstance(u, dict):
        return None
    return {
        "account_id": u.get("accountId") or u.get("name"),
        "name": u.get("displayName"),
        "email": u.get("emailAddress"),
    }


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str, *, timeout: float = 30.0) -> None:
        if not base_url:
            raise MissingCredentials("JIRA_BASE_URL is not set")
        if not email or not token:
            raise MissingCredentials("JIRA_EMAIL and JIRA_API_TOKEN must be set to call Jira")
        basic = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.base_url = base_url.rstrip("/")
        self.http = JsonHttp(
            self.base_url, {"Authorization": f"Basic {basic}"}, (token, basic), timeout=timeout
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JiraClient:
        env = os.environ if env is None else env
        return cls(
            env.get("JIRA_BASE_URL", ""), env.get("JIRA_EMAIL", ""), env.get("JIRA_API_TOKEN", "")
        )

    @staticmethod
    def _key(key: str) -> str:
        return urllib.parse.quote(key, safe="")

    def get_issue(self, key: str) -> dict[str, Any]:
        fields = "summary,description,assignee,status,labels,project,issuetype"
        data = self.http.request("GET", f"/rest/api/3/issue/{self._key(key)}?fields={fields}").data
        f = data.get("fields") or {}
        return {
            "key": data.get("key", key),
            "summary": f.get("summary") or "",
            "description": adf_to_text(f.get("description")).strip(),
            "assignee": _user(f.get("assignee")),
            "status": (f.get("status") or {}).get("name"),
            "labels": list(f.get("labels") or []),
            "project": (f.get("project") or {}).get("key"),
            "type": (f.get("issuetype") or {}).get("name"),
            "url": f"{self.base_url}/browse/{data.get('key', key)}",
        }

    def myself(self) -> dict[str, Any]:
        """The account the token belongs to (`accountId`, `displayName`, ...)."""
        return self.http.request("GET", "/rest/api/3/myself").data  # type: ignore[no-any-return]

    def search(
        self, jql: str, *, fields: tuple[str, ...] = ("summary", "assignee"), limit: int = 50
    ) -> list[dict[str, Any]]:
        """Issues matching `jql`, with their changelog. Uses Jira Cloud's
        `POST /rest/api/3/search/jql`, falling back to the older `GET /rest/api/3/search`
        on servers that don't have it."""
        body = {"jql": jql, "fields": list(fields), "expand": "changelog", "maxResults": limit}
        try:
            data = self.http.request("POST", "/rest/api/3/search/jql", body).data
        except PermanentApiError as e:
            if e.status not in (404, 405, 410):
                raise
            q = urllib.parse.urlencode(
                {"jql": jql, "fields": ",".join(fields), "expand": "changelog", "maxResults": limit}
            )
            data = self.http.request("GET", f"/rest/api/3/search?{q}").data
        return list((data or {}).get("issues") or [])

    def add_comment(self, key: str, body: str) -> dict[str, Any]:
        data = self.http.request(
            "POST", f"/rest/api/3/issue/{self._key(key)}/comment", {"body": text_to_adf(body)}
        ).data
        return {"id": str(data.get("id")), "key": key, "body": adf_to_text(data.get("body"))}

    def list_comments(self, key: str, *, max_pages: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        for _ in range(max_pages):
            path = f"/rest/api/3/issue/{self._key(key)}/comment?startAt={start}&maxResults=100"
            data = self.http.request("GET", path).data or {}
            page = data.get("comments") or []
            for c in page:
                out.append(
                    {
                        "id": str(c.get("id")),
                        "body": adf_to_text(c.get("body")),
                        "author": _user(c.get("author")),
                    }
                )
            start += len(page)
            if not page or start >= int(data.get("total", start)):
                break
        return out
