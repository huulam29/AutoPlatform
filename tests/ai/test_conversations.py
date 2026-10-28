"""Chat conversations kept on the server: save, list, search, the 7-day archive rule, API."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger, conversations
from agentline.api.server import create_server
from agentline.assistant import gather_live, live_text
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.errors import ValidationError

DAY = conversations.DAY
T0 = 1_700_000_000.0


def msgs(*texts: str) -> list[dict[str, object]]:
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": t}
            for i, t in enumerate(texts)]  # fmt: skip


def test_files_live_under_agentline_home() -> None:
    conversations.save("c-abc123", msgs("hi"), now=T0)
    home = Path(os.environ["AGENTLINE_HOME"])
    assert (home / "assistant" / "conversations" / "c-abc123.json").is_file()


def test_title_is_the_first_user_message_shortened() -> None:
    assert conversations.title_of([{"role": "assistant", "content": "hello"}]) == "New chat"
    assert conversations.title_of(msgs("  Build a\n pipeline  ")) == "Build a pipeline"
    long = conversations.title_of(msgs("x" * 200))
    assert len(long) == conversations.MAX_TITLE and long.endswith("…")


def test_archive_rule() -> None:
    c = {"updated_at": T0, "kept_at": None, "archived_at": None}
    assert not conversations.is_archived(c, T0 + 7 * DAY)  # exactly 7 days: still active
    assert conversations.is_archived(c, T0 + 7 * DAY + 1)
    assert not conversations.is_archived(c | {"kept_at": T0 + 6 * DAY}, T0 + 8 * DAY)
    assert conversations.is_archived(c | {"archived_at": T0 + 1}, T0 + 2)  # by hand
    # a message after archiving by hand brings it back
    assert not conversations.is_archived(c | {"archived_at": T0 - 5}, T0 + 1)


def test_save_keeps_created_and_moves_updated_only_on_change() -> None:
    conversations.save("c-abc123", msgs("hi"), now=T0)
    same = conversations.save("c-abc123", msgs("hi"), now=T0 + 50)
    assert same["updated_at"] == T0  # a re-save of the same list isn't activity
    more = conversations.save("c-abc123", msgs("hi", "hello"), now=T0 + 99)
    assert more["created_at"] == T0 and more["updated_at"] == T0 + 99


def test_save_checks_id_and_messages() -> None:
    with pytest.raises(ValidationError):
        conversations.save("../etc", msgs("hi"))
    with pytest.raises(ValidationError):
        conversations.save("c-abc123", [])
    assert conversations.get("../etc") is None


def test_list_newest_first_with_search_and_archive() -> None:
    conversations.save("c-old001", msgs("old talk about kafka"), now=T0)
    conversations.save("c-new001", msgs("fresh", "the answer mentions Kafka"), now=T0 + 9 * DAY)
    now = T0 + 9 * DAY + 10
    rows = conversations.list_all(now=now)
    assert [r["id"] for r in rows] == ["c-new001", "c-old001"]
    assert [r["archived"] for r in rows] == [False, True]
    assert rows[0]["messages"] == 2 and "messages" in rows[0]
    assert [r["id"] for r in conversations.list_all("KAFKA", now=now)] == ["c-new001", "c-old001"]
    assert [r["id"] for r in conversations.list_all("fresh", now=now)] == ["c-new001"]


def test_unarchive_keeps_it_active_for_another_week_and_never_deletes() -> None:
    conversations.save("c-old001", msgs("old"), now=T0)
    c = conversations.set_archived("c-old001", False, now=T0 + 30 * DAY)
    assert not conversations.is_archived(c, T0 + 31 * DAY)
    assert conversations.is_archived(c, T0 + 38 * DAY)
    c = conversations.set_archived("c-old001", True, now=T0 + 31 * DAY)
    assert conversations.is_archived(c, T0 + 31 * DAY)
    assert conversations.get("c-old001")["messages"] == msgs("old")
    with pytest.raises(KeyError):
        conversations.set_archived("c-ghost1", True)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    cfg = Config()
    app = App(SqliteLedger(tmp_path / "l.db"))
    server = create_server(
        app, [], pipelines_dir=pipelines, registry=default_registry(cfg), config=cfg
    )
    return TestClient(server)


def test_api_round_trip(client: TestClient) -> None:
    assert client.get("/api/assistant/conversations").json() == {
        "conversations": [],
        "archive_days": 7,
    }
    image = {"role": "user", "content": "x", "attachments": ["a" * 32 + ".png"]}
    body = {"messages": [*msgs("Build a pipeline", "Sure"), image]}
    r = client.put("/api/assistant/conversations/c-web0001", json=body)
    assert r.status_code == 200 and r.json()["title"] == "Build a pipeline"
    got = client.get("/api/assistant/conversations/c-web0001").json()
    assert got["messages"] == body["messages"] and got["archived"] is False
    [row] = client.get("/api/assistant/conversations", params={"q": "pipeline"}).json()[
        "conversations"
    ]
    assert row["id"] == "c-web0001"
    assert client.post("/api/assistant/conversations/c-web0001/archive").json()["archived"]
    assert not client.post("/api/assistant/conversations/c-web0001/unarchive").json()["archived"]
    assert client.get("/api/assistant/conversations/c-nope01").status_code == 404
    assert client.post("/api/assistant/conversations/c-nope01/archive").status_code == 404
    assert client.post("/api/assistant/conversations/c-web0001/delete").status_code == 404
    bad = client.put("/api/assistant/conversations/bad%20id", json=body)
    assert bad.status_code == 422
    empty = client.put("/api/assistant/conversations/c-web0002", json={"messages": []})
    assert empty.status_code == 422


def test_live_state_lists_conversations(tmp_path: Path) -> None:
    conversations.save("c-live001", msgs("Set up keys"))

    class Files:
        root = tmp_path
        config = None

    text = live_text(gather_live(Files()))
    assert "CONVERSATIONS" in text and "c-live001: Set up keys (active" in text
