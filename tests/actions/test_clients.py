"""JiraClient / GitHubClient against the fake servers: parsing, pagination, errors, redaction."""

from __future__ import annotations

import base64

import pytest

from agentline.errors import PermanentError
from agentline.integrations import (
    ApiError,
    GitHubClient,
    JiraClient,
    MissingCredentials,
    PermanentApiError,
)
from agentline.integrations.jira import adf_to_text, text_to_adf

from .fakes import GITHUB_TOKEN, JIRA_EMAIL, FakeState, jira_issue


def test_jira_get_issue_flattens_description(fake: tuple[str, FakeState]) -> None:
    url, state = fake
    state.issues["PROJ-1"] = jira_issue("PROJ-1", description="First para.\n\nSecond.")
    issue = JiraClient.from_env().get_issue("PROJ-1")
    assert issue["key"] == "PROJ-1"
    assert issue["summary"] == "Login broken"
    assert issue["description"] == "First para.\n\nSecond."
    assert issue["assignee"] == {"account_id": "acc-1", "name": "Dev One", "email": None}
    assert issue["status"] == "In Progress"
    assert issue["labels"] == ["backend"]
    assert issue["url"] == f"{url}/browse/PROJ-1"


def test_jira_comments_paginate(fake: tuple[str, FakeState]) -> None:
    _, state = fake
    state.issues["PROJ-1"] = jira_issue("PROJ-1")
    jira = JiraClient.from_env()
    for i in range(5):  # the fake pages 2 at a time
        jira.add_comment("PROJ-1", f"comment {i}\nline two")
    bodies = [c["body"] for c in jira.list_comments("PROJ-1")]
    assert len(bodies) == 5
    assert bodies[4].startswith("comment 4\nline two")


def test_adf_round_trip() -> None:
    assert adf_to_text(text_to_adf("a\nb\n\nc")).strip() == "a\nb\n\nc"
    assert adf_to_text("plain") == "plain"


def test_missing_credentials_are_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingCredentials, match="JIRA_BASE_URL"):
        JiraClient.from_env()
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.invalid")
    with pytest.raises(MissingCredentials, match="JIRA_API_TOKEN"):
        JiraClient.from_env()
    with pytest.raises(MissingCredentials, match="GITHUB_TOKEN"):
        GitHubClient.from_env()
    assert issubclass(MissingCredentials, PermanentError)


def test_tokens_are_redacted_from_errors(fake: tuple[str, FakeState]) -> None:
    url, state = fake
    state.issues["PROJ-1"] = jira_issue("PROJ-1")
    wrong = "wrong-token-but-secret"
    with pytest.raises(PermanentApiError) as e:
        JiraClient(url, JIRA_EMAIL, wrong).get_issue("PROJ-1")
    basic = base64.b64encode(f"{JIRA_EMAIL}:{wrong}".encode()).decode()
    assert "401" in str(e.value)
    assert wrong not in str(e.value) and basic not in str(e.value)
    assert "***" in str(e.value)

    with pytest.raises(PermanentApiError) as e2:
        GitHubClient("ghp_wrong_secret_token", url).list_pulls("acme/app")
    assert "ghp_wrong_secret_token" not in str(e2.value)
    assert state.auth_failures == 2


def test_server_errors_are_retryable(fake: tuple[str, FakeState]) -> None:
    _, state = fake
    state.issues["PROJ-1"] = jira_issue("PROJ-1")
    state.fail_next_comment_after_write = True
    with pytest.raises(ApiError) as e:
        JiraClient.from_env().add_comment("PROJ-1", "hi")
    assert not isinstance(e.value, PermanentError)
    assert e.value.status == 503


def test_unreachable_host_is_retryable_and_redacted() -> None:
    with pytest.raises(ApiError) as e:
        GitHubClient(GITHUB_TOKEN, "http://127.0.0.1:1").list_pulls("acme/app")
    assert not isinstance(e.value, PermanentError)
    assert GITHUB_TOKEN not in str(e.value)


def test_github_pulls_and_paginated_reviews(fake: tuple[str, FakeState]) -> None:
    _, state = fake
    gh = GitHubClient.from_env()
    pr = gh.create_pull("acme/app", head="feat", base="main", title="T")
    assert pr["number"] == 1
    assert gh.list_pulls("acme/app", head="acme:feat")[0]["number"] == 1
    assert gh.list_pulls("acme/app", head="acme:other") == []
    assert gh.get_pull("acme/app", 1)["title"] == "T"
    for i in range(5):
        state.add_review("acme/app", 1, "commented", body=f"r{i}", comments=[{"body": f"c{i}"}])
    reviews = gh.list_reviews("acme/app", 1)  # 3 pages of 2
    assert [r["body"] for r in reviews] == ["r0", "r1", "r2", "r3", "r4"]
    assert len(gh.list_review_comments("acme/app", 1)) == 5
    assert gh.list_review_comments("acme/app", 1, reviews[2]["id"])[0]["body"] == "c2"


def test_bad_repo_slug() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        GitHubClient(GITHUB_TOKEN, "http://x.invalid").list_pulls("not-a-slug")
