# API contract: identities and people

All under `/api`, all behind the identity dependency except `/auth/check`.

## GET /auth/check (changed)
`{"token_required": bool, "ok": bool, "identity": {"kind", "name", "admin"} | null}` for the
request's bearer. Never echoes the token.

## GET /people
Admin: `{"people": [{"name", "admin", "created_at"}], "me": {identity}}`.
Non-admin: only their own row. Never a token or hash.

## POST /people  `{"name": str, "admin": bool = false}`
Admin only (403). 201 `{"person": {...}, "token": "alp_..."}` (the only time it is returned).
409 name taken; 422 bad name.

## POST /people/{name}/token
Admin only. `{"person": {...}, "token": "alp_..."}`; the old token stops working. 404 unknown.

## PUT /people/{name}/admin  `{"admin": bool}`
Admin only. `{"person": {...}}`.

## DELETE /people/{name}
Admin only. `{"revoked": name}`. 404 unknown.

## Changed responses
`POST /runs/{id}/approve|reject` `actor` = the request identity's name.
