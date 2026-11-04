"""`agentline users` (spec 007): add / list / token / admin / revoke."""

from __future__ import annotations

from agentline.people import PeopleStore

from .helpers import Cli


def test_add_prints_the_token_once_and_list_never_shows_it(cli: Cli) -> None:
    out = cli.ok("users", "add", "alice", "--admin")
    token = out.strip().splitlines()[-1]
    assert token.startswith("alp_")
    assert PeopleStore().match(token).name == "alice"
    listing = cli.ok("users", "list")
    assert "alice" in listing and "admin" in listing and token not in listing
    rows = cli.json("users", "list")
    assert rows[0]["name"] == "alice" and set(rows[0]) == {"name", "admin", "created_at"}


def test_token_admin_revoke(cli: Cli) -> None:
    old = cli.ok("users", "add", "bob").strip().splitlines()[-1]
    new = cli.ok("users", "token", "bob").strip().splitlines()[-1]
    store = PeopleStore()
    assert store.match(old) is None and store.match(new) is not None
    assert "bob: admin" in cli.ok("users", "admin", "bob", "on")
    assert store.get("bob").admin
    cli.ok("users", "revoke", "bob")
    assert store.match(new) is None
    actions = [a["action"] for a in cli.json("audit")]
    assert {"person.add", "person.token", "person.admin", "person.revoke"} <= set(actions)
    assert new not in cli.ok("audit")


def test_errors(cli: Cli) -> None:
    code, _, err = cli.run("users", "add", "shared-token")
    assert code == 1 and "reserved" in err
    code, _, err = cli.run("users", "revoke", "nobody")
    assert code == 1 and "no person" in err
