"""A minimal stdio MCP server for tests: answers initialize and tools/list."""

import json
import os
import sys

for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        out = {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "fake", "version": "1"},
            "capabilities": {"tools": {}},
        }
    elif msg.get("method") == "tools/list":
        token = os.environ.get("FAKE_TOKEN", "")
        out = {"tools": [{"name": "search", "description": f"token ok: {token == 'tok-123'}"}]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": out}), flush=True)
