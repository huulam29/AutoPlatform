"""Who a request is from (spec 007): a person, the shared API token, or the local user.

One dependency on every `/api` router resolves the bearer token and puts the result on
`request.state.identity`; routes that write audit rows take the actor from it (`actor_of`), so
an approval from Alice's browser is recorded as `alice`, not as the engine's OS user.

- A person's token -> `person`, their name and admin flag.
- The shared `AGENTLINE_API_TOKEN` from another machine -> `shared`, `shared-token`, admin.
- The shared token from the engine's own machine (a loopback client with no proxy headers: the
  Mac app, which always sends it, or a browser on the server) -> `local`, the OS user, admin.
- No bearer while no shared token is set (a loopback-only engine) -> `local`, the OS user, admin.
- Anything else -> 401. A bearer that matches nobody is refused even on an open engine, so a
  mistyped or revoked personal token is noticed instead of silently acting as the local user.
"""

from __future__ import annotations

import hmac
from dataclasses import asdict, dataclass
from typing import Any, Literal

from fastapi import HTTPException, Request

from agentline.app import default_actor
from agentline.people import PeopleStore

SHARED_NAME = "shared-token"


@dataclass(frozen=True)
class Identity:
    kind: Literal["local", "shared", "person"]
    name: str
    admin: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


_PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-real-ip")


def from_this_machine(request: Request) -> bool:
    """A loopback client that didn't come through a proxy (same rule as `api.app`)."""
    from agentline.api.server import is_local

    host = request.client.host if request.client else ""
    if not host or not is_local(host):
        return False
    return not any(h in request.headers for h in _PROXY_HEADERS)


def resolve(
    given: str, shared_token: str | None, people: PeopleStore | None, *, local: bool = False
) -> Identity | None:
    """The identity for a bearer value (`""` for none), or None when it is refused. `local`:
    the request comes from the engine's own machine (`from_this_machine`)."""
    if not given:
        if shared_token:
            return None
        return Identity("local", default_actor(), True)
    if shared_token and hmac.compare_digest(given.encode(), shared_token.encode()):
        if local:
            return Identity("local", default_actor(), True)
        return Identity("shared", SHARED_NAME, True)
    person = people.match(given) if people is not None else None
    if person is not None:
        return Identity("person", person.name, person.admin)
    return None


def identity_dependency(shared_token: str | None, people: PeopleStore | None) -> Any:
    def check(request: Request) -> Identity:
        who = resolve(bearer(request), shared_token, people, local=from_this_machine(request))
        if who is None:
            raise HTTPException(
                401, "missing or bad bearer token", headers={"WWW-Authenticate": "Bearer"}
            )
        request.state.identity = who
        return who

    return check


def identity_of(request: Request) -> Identity:
    """The request's identity. Routes outside the dependency (tests building a router alone)
    fall back to the local user, as before named identities."""
    who = getattr(getattr(request, "state", None), "identity", None)
    return who if isinstance(who, Identity) else Identity("local", default_actor(), True)


def actor_of(request: Request) -> str:
    """The name to write in audit rows for this request."""
    return identity_of(request).name


def require_admin(request: Request) -> Identity:
    who = identity_of(request)
    if not who.admin:
        raise HTTPException(403, "only an admin can manage people")
    return who
