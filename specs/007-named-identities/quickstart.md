# Quickstart: Named Identities

```bash
uv run agentline users add alice --admin     # prints alice's token once
uv run agentline users list
uv sync --extra server && uv run agentline serve --host 0.0.0.0
```

In a browser: sign in with alice's token, approve a gate, then:

```bash
uv run agentline audit --run <run_id>         # actor: alice
uv run agentline users revoke alice           # alice's next request gets 401
```
