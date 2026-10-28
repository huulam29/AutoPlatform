"""GitHub REST client (stdlib urllib). Credentials from env vars only: GITHUB_TOKEN, and
GITHUB_API_URL (default https://api.github.com; set it for GitHub Enterprise or a fake)."""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from typing import Any

from agentline.integrations.http import JsonHttp, MissingCredentials

DEFAULT_API_URL = "https://api.github.com"


def split_repo(repo: str) -> tuple[str, str]:
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError(f"expected a GitHub repo as 'owner/name', got {repo!r}")
    return owner, name


def pr_summary(pr: dict[str, Any], repo: str) -> dict[str, Any]:
    head = pr.get("head") or {}
    return {
        "number": pr["number"],
        "url": pr.get("html_url") or pr.get("url"),
        "branch": head.get("ref"),
        "repo": repo,
        "state": pr.get("state"),
        "head_sha": head.get("sha"),
        "base": (pr.get("base") or {}).get("ref"),
    }


class GitHubClient:
    def __init__(self, token: str, api_url: str = DEFAULT_API_URL, *, timeout: float = 30.0):
        if not token:
            raise MissingCredentials("GITHUB_TOKEN must be set to call GitHub")
        self.http = JsonHttp(
            api_url or DEFAULT_API_URL,
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            (token,),
            timeout=timeout,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GitHubClient:
        env = os.environ if env is None else env
        return cls(env.get("GITHUB_TOKEN", ""), env.get("GITHUB_API_URL") or DEFAULT_API_URL)

    @staticmethod
    def _repo(repo: str) -> str:
        owner, name = split_repo(repo)
        return f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"

    def get_repo(self, repo: str) -> dict[str, Any]:
        return self.http.request("GET", self._repo(repo)).data  # type: ignore[no-any-return]

    def list_pulls(
        self, repo: str, *, head: str | None = None, state: str = "open"
    ) -> list[dict[str, Any]]:
        """`head` is `owner:branch` (GitHub's filter format)."""
        q = {"state": state, "per_page": "100"}
        if head:
            q["head"] = head
        return self.http.get_all(f"{self._repo(repo)}/pulls?{urllib.parse.urlencode(q)}")

    def create_pull(
        self,
        repo: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str = "",
        draft: bool = False,
    ) -> dict[str, Any]:
        payload = {"head": head, "base": base, "title": title, "body": body, "draft": draft}
        return self.http.request("POST", f"{self._repo(repo)}/pulls", payload).data  # type: ignore[no-any-return]

    def get_pull(self, repo: str, number: int) -> dict[str, Any]:
        return self.http.request("GET", f"{self._repo(repo)}/pulls/{int(number)}").data  # type: ignore[no-any-return]

    def merge_pull(
        self, repo: str, number: int, *, method: str = "squash", sha: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"merge_method": method}
        if sha:
            payload["sha"] = sha  # refuse if the head moved since it was approved
        return self.http.request(
            "PUT", f"{self._repo(repo)}/pulls/{int(number)}/merge", payload
        ).data  # type: ignore[no-any-return]

    def list_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.http.get_all(f"{self._repo(repo)}/pulls/{int(number)}/reviews?per_page=100")

    def list_review_comments(
        self, repo: str, number: int, review_id: int | None = None
    ) -> list[dict[str, Any]]:
        base = f"{self._repo(repo)}/pulls/{int(number)}"
        path = f"{base}/reviews/{int(review_id)}/comments" if review_id else f"{base}/comments"
        return self.http.get_all(f"{path}?per_page=100")

    def create_issue(
        self, repo: str, *, title: str, body: str = "", labels: list[str] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        return self.http.request("POST", f"{self._repo(repo)}/issues", payload).data  # type: ignore[no-any-return]

    def comment_issue(self, repo: str, number: int, body: str) -> dict[str, Any]:
        return self.http.request(
            "POST", f"{self._repo(repo)}/issues/{int(number)}/comments", {"body": body}
        ).data  # type: ignore[no-any-return]
