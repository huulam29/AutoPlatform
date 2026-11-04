"""People store (spec 007): tokens shown once, only hashes kept, owner-only, atomic."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from agentline.errors import ValidationError
from agentline.people import PeopleError, PeopleStore, check_person_name, token_hash


def test_add_returns_a_token_once_and_keeps_only_its_hash(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path / "people.json")
    person, token = store.add("alice", admin=True)
    assert token.startswith("alp_") and len(token) > 40
    text = (tmp_path / "people.json").read_text()
    assert token not in text
    assert token_hash(token) in text
    assert person.public() == {"name": "alice", "admin": True, "created_at": person.created_at}
    assert "token_sha256" not in person.public()
    assert store.match(token) == person
    assert store.match(token + "x") is None
    assert store.match("") is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_file_is_owner_only(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path / "people.json")
    store.add("alice")
    mode = stat.S_IMODE(os.stat(tmp_path / "people.json").st_mode)
    assert mode == 0o600


def test_reissue_replaces_the_token(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path / "people.json")
    _, old = store.add("bob")
    _, new = store.reissue("BOB")  # names match case-insensitively
    assert store.match(old) is None
    assert store.match(new) is not None and store.match(new).name == "bob"


def test_revoke_and_admin(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path / "people.json")
    _, token = store.add("carol")
    assert store.set_admin("carol", True).admin
    store.revoke("carol")
    assert store.match(token) is None
    with pytest.raises(PeopleError, match="no person"):
        store.revoke("carol")


def test_a_second_store_sees_changes_without_restart(tmp_path: Path) -> None:
    """The CLI writes; the running server's store sees it on the next lookup."""
    server = PeopleStore(tmp_path / "people.json")
    cli = PeopleStore(tmp_path / "people.json")
    _, token = cli.add("dave")
    assert server.match(token) is not None
    cli.revoke("dave")
    assert server.match(token) is None


@pytest.mark.parametrize(
    "name", ["", "a b", "shared-token", "Shared-Token", "local", "x" * 65, "tab\t", "é"]
)
def test_bad_names(name: str) -> None:
    with pytest.raises(ValidationError):
        check_person_name(name)


def test_names_are_unique_case_insensitively(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path / "people.json")
    store.add("Erin")
    with pytest.raises(PeopleError, match="already"):
        store.add("erin")


def test_corrupt_file_matches_nobody_and_refuses_writes(tmp_path: Path) -> None:
    path = tmp_path / "people.json"
    store = PeopleStore(path)
    _, token = store.add("frank")
    path.write_text("{not json")
    assert store.match(token) is None
    assert store.list() == []
    with pytest.raises(PeopleError, match="unreadable"):
        store.add("gina")
    assert path.read_text() == "{not json"  # never overwritten


def test_file_format(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path / "people.json")
    store.add("hank")
    data = json.loads((tmp_path / "people.json").read_text())
    assert data["version"] == 1
    assert set(data["people"][0]) == {"name", "admin", "created_at", "token_sha256"}
