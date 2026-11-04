"""People who use the API under their own name (spec 007, Settings > People).

GET    /people                  {people: [{name, admin, created_at}], me}  (non-admins: themselves)
POST   /people                  {name, admin?} -> 201 {person, token}  (the only time it is shown)
POST   /people/{name}/token     {person, token}: a new token; the old one stops working
PUT    /people/{name}/admin     {admin} -> {person}
DELETE /people/{name}           {revoked}

Writes need an admin identity (the local user, the shared token, or a person marked admin) and
write an audit row naming who did it; never the token. A token just issued is taught to the
running redactor, so it is masked if it ever reaches a log.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agentline.api.identity import identity_of, require_admin
from agentline.app import App
from agentline.errors import ValidationError
from agentline.people import PeopleError, PeopleStore


class AddBody(BaseModel):
    name: str
    admin: bool = False


class AdminBody(BaseModel):
    admin: bool


def create_people_router(app: App, people: PeopleStore) -> APIRouter:
    router = APIRouter()

    def _fail(e: ValidationError) -> HTTPException:
        msg = str(e)
        if msg.startswith("no person"):
            return HTTPException(404, msg)
        if "already" in msg:
            return HTTPException(409, msg)
        if isinstance(e, PeopleError):
            return HTTPException(409, msg)
        return HTTPException(422, msg)

    def _issued(name: str, token: str) -> None:
        add = getattr(app.redactor, "add", None)
        if callable(add):
            add(f"PERSON_TOKEN_{name.upper()}", token)

    @router.get("/people")
    def list_people(request: Request) -> dict[str, Any]:
        me = identity_of(request)
        rows = [p.public() for p in people.list()]
        if not me.admin:
            rows = [r for r in rows if me.kind == "person" and r["name"] == me.name]
        return {"people": rows, "me": me.as_dict()}

    @router.post("/people", status_code=201)
    def add_person(body: AddBody, request: Request) -> dict[str, Any]:
        who = require_admin(request)
        try:
            person, token = people.add(body.name.strip(), admin=body.admin)
        except ValidationError as e:
            raise _fail(e) from None
        _issued(person.name, token)
        app.audit("person.add", {"person": person.name, "admin": person.admin}, actor=who.name)
        return {"person": person.public(), "token": token}

    @router.post("/people/{name}/token")
    def reissue(name: str, request: Request) -> dict[str, Any]:
        who = require_admin(request)
        try:
            person, token = people.reissue(name)
        except ValidationError as e:
            raise _fail(e) from None
        _issued(person.name, token)
        app.audit("person.token", {"person": person.name}, actor=who.name)
        return {"person": person.public(), "token": token}

    @router.put("/people/{name}/admin")
    def set_admin(name: str, body: AdminBody, request: Request) -> dict[str, Any]:
        who = require_admin(request)
        try:
            person = people.set_admin(name, body.admin)
        except ValidationError as e:
            raise _fail(e) from None
        app.audit("person.admin", {"person": person.name, "admin": person.admin}, actor=who.name)
        return {"person": person.public()}

    @router.delete("/people/{name}")
    def revoke(name: str, request: Request) -> dict[str, Any]:
        who = require_admin(request)
        try:
            person = people.revoke(name)
        except ValidationError as e:
            raise _fail(e) from None
        app.audit("person.revoke", {"person": person.name}, actor=who.name)
        return {"revoked": person.name}

    return router
