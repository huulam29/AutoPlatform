"""Named identities (spec 007): audit rows written through the API name the person."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api import create_server
from agentline.pipeline import load_pipeline

from ..pipeline.fakes import World, make_registry
from ..pipeline.test_spec import EXAMPLE
from .test_runs_api import to_gate

SHARED = "shared-secret-token-for-tests-0123456789"


@pytest.fixture
def app(tmp_path: Path) -> Iterator[App]:
    ledger = SqliteLedger(tmp_path / "ledger.db")
    app = App(ledger, backoff_base=0.0, artifact_root=tmp_path / "artifacts")
    app.register_pipeline(load_pipeline(EXAMPLE, make_registry(World())))
    yield app
    ledger.close()


def _server(app: App, tmp_path: Path, token: str) -> TestClient:
    pdir = tmp_path / "pipelines"
    if not pdir.exists():
        shutil.copytree(EXAMPLE.parent, pdir / "ticket-to-eod")
    return TestClient(create_server(app, [], pipelines_dir=pdir, token=token))


@pytest.fixture
def open_client(app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A loopback-only engine: no shared token."""
    monkeypatch.delenv("AGENTLINE_API_TOKEN", raising=False)
    monkeypatch.setenv("AGENTLINE_USER", "osuser")
    with _server(app, tmp_path, "") as c:
        yield c


@pytest.fixture
def locked_client(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """An engine reachable off localhost: the shared token is set."""
    monkeypatch.setenv("AGENTLINE_USER", "osuser")
    with _server(app, tmp_path, SHARED) as c:
        yield c


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def add_person(c: TestClient, name: str, admin: bool = False, auth: str = SHARED) -> str:
    r = c.post("/api/people", json={"name": name, "admin": admin}, headers=bearer(auth))
    assert r.status_code == 201, r.text
    return r.json()["token"]


def last_audit(app: App, action: str) -> str:
    rows = [a for a in app.audit_log() if a.action == action]
    assert rows, f"no {action} audit row"
    return rows[-1].actor


# -- US1: audit rows name the person ---------------------------------------------------------
def test_approve_names_the_person(app: App, locked_client: TestClient) -> None:
    alice = add_person(locked_client, "alice")
    run_id = to_gate(app)
    r = locked_client.post(f"/api/runs/{run_id}/approve", json={}, headers=bearer(alice))
    assert r.status_code == 200, r.text
    assert r.json()["actor"] == "alice"
    assert last_audit(app, "approve") == "alice"


def test_reject_names_the_person(app: App, locked_client: TestClient) -> None:
    bob = add_person(locked_client, "bob")
    run_id = to_gate(app)
    r = locked_client.post(f"/api/runs/{run_id}/reject", json={}, headers=bearer(bob))
    assert r.status_code == 200, r.text
    assert last_audit(app, "reject") == "bob"


def test_retry_names_the_person(app: App, locked_client: TestClient) -> None:
    carol = add_person(locked_client, "carol")
    run_id = to_gate(app)
    r = locked_client.post(f"/api/runs/{run_id}/retry", json={}, headers=bearer(carol))
    assert r.status_code == 200, r.text
    assert last_audit(app, "retry") == "carol"


def test_shared_token_is_recorded_as_shared_token(app: App, locked_client: TestClient) -> None:
    run_id = to_gate(app)
    r = locked_client.post(f"/api/runs/{run_id}/approve", json={}, headers=bearer(SHARED))
    assert r.json()["actor"] == "shared-token"
    assert last_audit(app, "approve") == "shared-token"


def test_shared_token_from_this_machine_is_the_os_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Mac app always sends the shared token over loopback: its approvals stay the Mac
    user's, as before named identities."""
    from agentline.api.identity import resolve

    monkeypatch.setenv("AGENTLINE_USER", "osuser")
    who = resolve(SHARED, SHARED, None, local=True)
    assert who is not None and (who.kind, who.name) == ("local", "osuser")
    assert resolve(SHARED, SHARED, None, local=False).name == "shared-token"
    assert resolve("", SHARED, None, local=True) is None  # still needs the token


def test_from_this_machine() -> None:
    from types import SimpleNamespace

    from agentline.api.identity import from_this_machine

    def req(host: str, headers: dict[str, str] | None = None) -> object:
        return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers or {})

    assert from_this_machine(req("127.0.0.1"))
    assert from_this_machine(req("::1"))
    assert not from_this_machine(req("10.0.0.5"))
    assert not from_this_machine(req("127.0.0.1", {"x-forwarded-for": "10.0.0.5"}))
    assert not from_this_machine(req("testclient"))


def test_loopback_without_token_is_the_os_user(app: App, open_client: TestClient) -> None:
    run_id = to_gate(app)
    r = open_client.post(f"/api/runs/{run_id}/approve", json={})
    assert r.json()["actor"] == "osuser"
    assert last_audit(app, "approve") == "osuser"


def test_personal_token_on_an_open_engine_names_the_person(
    app: App, open_client: TestClient
) -> None:
    r = open_client.post("/api/people", json={"name": "dana"})  # the local user is admin
    token = r.json()["token"]
    run_id = to_gate(app)
    r = open_client.post(f"/api/runs/{run_id}/approve", json={}, headers=bearer(token))
    assert r.json()["actor"] == "dana"


def test_unknown_bearer_is_refused_even_on_an_open_engine(open_client: TestClient) -> None:
    assert open_client.get("/api/runs", headers=bearer("alp_nope")).status_code == 401


def test_card_apply_uses_the_person(
    app: App, locked_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentline import runchat

    seen: dict[str, str] = {}

    async def fake_apply(app, run_id, message_id, index, *, actor, dismiss=False):
        seen["actor"] = actor
        return {"done": True}

    monkeypatch.setattr(runchat, "apply_card", fake_apply)
    erin = add_person(locked_client, "erin")
    r = locked_client.post("/api/runs/r1/messages/1/apply", json={"index": 0}, headers=bearer(erin))
    assert r.status_code == 200, r.text
    assert seen["actor"] == "erin"


def test_no_api_route_uses_the_os_user_directly() -> None:
    """Every audited API route takes the actor from the request (`actor_of`)."""
    api = Path(__file__).resolve().parents[2] / "src" / "agentline" / "api"
    offenders = [
        p.name
        for p in api.glob("*.py")
        if p.name != "identity.py" and "default_actor(" in p.read_text()
    ]
    assert offenders == []


# -- sign-in check ---------------------------------------------------------------------------
def test_auth_check_reports_who(locked_client: TestClient) -> None:
    frank = add_person(locked_client, "frank", admin=True)
    r = locked_client.get("/api/auth/check", headers=bearer(frank)).json()
    assert r == {
        "token_required": True,
        "ok": True,
        "identity": {"kind": "person", "name": "frank", "admin": True},
    }
    assert frank not in str(r)
    r = locked_client.get("/api/auth/check", headers=bearer("wrong")).json()
    assert r["ok"] is False and r["identity"] is None
    r = locked_client.get("/api/auth/check", headers=bearer(SHARED)).json()
    assert r["identity"]["name"] == "shared-token"


# -- US3: revoke at once ---------------------------------------------------------------------
def test_revoke_takes_effect_on_the_next_request(app: App, locked_client: TestClient) -> None:
    gina = add_person(locked_client, "gina")
    assert locked_client.get("/api/runs", headers=bearer(gina)).status_code == 200
    r = locked_client.delete("/api/people/gina", headers=bearer(SHARED))
    assert r.json() == {"revoked": "gina"}
    assert locked_client.get("/api/runs", headers=bearer(gina)).status_code == 401
    assert last_audit(app, "person.revoke") == "shared-token"


def test_revoke_from_the_cli_reaches_the_running_server(locked_client: TestClient) -> None:
    from agentline.people import PeopleStore

    hank = add_person(locked_client, "hank")
    PeopleStore().revoke("hank")  # another process: same $AGENTLINE_HOME file
    assert locked_client.get("/api/runs", headers=bearer(hank)).status_code == 401


def test_reissue(app: App, locked_client: TestClient) -> None:
    old = add_person(locked_client, "ivan")
    r = locked_client.post("/api/people/ivan/token", headers=bearer(SHARED))
    new = r.json()["token"]
    assert new != old
    assert locked_client.get("/api/runs", headers=bearer(old)).status_code == 401
    assert locked_client.get("/api/runs", headers=bearer(new)).status_code == 200
    assert last_audit(app, "person.token") == "shared-token"


def test_past_audit_rows_keep_a_revoked_name(app: App, locked_client: TestClient) -> None:
    jo = add_person(locked_client, "jo")
    run_id = to_gate(app)
    locked_client.post(f"/api/runs/{run_id}/approve", json={}, headers=bearer(jo))
    locked_client.delete("/api/people/jo", headers=bearer(SHARED))
    assert last_audit(app, "approve") == "jo"


# -- US4: only admins manage people ----------------------------------------------------------
def test_non_admin_cannot_manage_people(locked_client: TestClient) -> None:
    kim = add_person(locked_client, "kim")
    add_person(locked_client, "lee")
    h = bearer(kim)
    assert locked_client.post("/api/people", json={"name": "x"}, headers=h).status_code == 403
    assert locked_client.post("/api/people/lee/token", headers=h).status_code == 403
    assert (
        locked_client.put("/api/people/kim/admin", json={"admin": True}, headers=h).status_code
        == 403
    )
    assert locked_client.delete("/api/people/lee", headers=h).status_code == 403
    r = locked_client.get("/api/people", headers=h).json()
    assert [p["name"] for p in r["people"]] == ["kim"]
    assert r["me"] == {"kind": "person", "name": "kim", "admin": False}


def test_admin_person_can_manage_people(app: App, locked_client: TestClient) -> None:
    mo = add_person(locked_client, "mo", admin=True)
    add_person(locked_client, "ned", auth=mo)
    assert last_audit(app, "person.add") == "mo"
    r = locked_client.get("/api/people", headers=bearer(mo)).json()
    assert {p["name"] for p in r["people"]} == {"mo", "ned"}


def test_people_errors(locked_client: TestClient) -> None:
    h = bearer(SHARED)
    add_person(locked_client, "oz")
    assert locked_client.post("/api/people", json={"name": "OZ"}, headers=h).status_code == 409
    assert (
        locked_client.post("/api/people", json={"name": "shared-token"}, headers=h).status_code
        == 422
    )
    assert locked_client.post("/api/people", json={"name": "a b"}, headers=h).status_code == 422
    assert locked_client.delete("/api/people/nobody", headers=h).status_code == 404


# -- SC-003: the token appears nowhere after it is issued ------------------------------------
def test_token_leaks_nowhere(app: App, locked_client: TestClient, tmp_path: Path) -> None:
    import os

    pat = add_person(locked_client, "pat", admin=True)
    run_id = to_gate(app)
    h = bearer(pat)
    seen = [
        locked_client.post(f"/api/runs/{run_id}/approve", json={}, headers=h).text,
        locked_client.get("/api/people", headers=h).text,
        locked_client.get("/api/auth/check", headers=h).text,
        locked_client.get(f"/api/runs/{run_id}", headers=h).text,
        locked_client.get("/api/settings", headers=h).text,
    ]
    assert all(pat not in s for s in seen)
    assert pat not in str([(a.actor, a.action, a.detail) for a in app.audit_log()])
    home = Path(os.environ["AGENTLINE_HOME"])
    for f in [*home.rglob("*"), *tmp_path.rglob("*")]:
        if f.is_file():
            assert pat.encode() not in f.read_bytes(), f
    assert app.redactor.redact(f"token={pat}") != f"token={pat}"
