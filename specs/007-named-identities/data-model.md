# Data Model: Named Identities

## Person (`$AGENTLINE_HOME/people.json`)

```json
{"version": 1, "people": [
  {"name": "alice", "admin": true, "created_at": 1793700000.0, "token_sha256": "<64 hex>"}
]}
```

- `name`: `^[A-Za-z0-9._@-]{1,64}$`, unique case-insensitively, not `shared-token` / `local`.
- `token_sha256`: SHA-256 hex of the current token. Never returned by the API or CLI.
- Revoke removes the entry. Reissue replaces the hash.
- File mode 0600, written via temp file + `os.replace`.

## Identity (per request, not stored)

| kind | name | admin | when |
|------|------|-------|------|
| `local` | `$AGENTLINE_USER` / `$USER` | true | no bearer and no shared token configured |
| `shared` | `shared-token` | true | bearer equals `AGENTLINE_API_TOKEN`, from another machine |
| `local` | `$AGENTLINE_USER` / `$USER` | true | bearer equals `AGENTLINE_API_TOKEN`, from loopback with no proxy headers (the Mac app) |
| `person` | person's name | person's flag | bearer's SHA-256 matches a person |

## Audit rows added

| action | actor | detail |
|--------|-------|--------|
| `person.add` | identity name | `{"person": name, "admin": bool}` |
| `person.token` | identity name | `{"person": name}` |
| `person.admin` | identity name | `{"person": name, "admin": bool}` |
| `person.revoke` | identity name | `{"person": name}` |
