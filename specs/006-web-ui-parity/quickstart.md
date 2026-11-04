# Quickstart: Checking Web UI Parity

Use a throwaway data folder (constitution: verification never writes to the real one).
Run the engine on macOS, Linux or WSL (it doesn't start on Windows yet, research R16).

```bash
export AGENTLINE_HOME=$(mktemp -d)
uv sync --extra server
(cd web && npm ci && npm run build)
uv run agentline serve --pipelines "$AGENTLINE_HOME/pipelines"
```

Open `http://127.0.0.1:8080` in a browser.

| # | Do | Expect |
|---|---|---|
| 1 | Stop the engine (Ctrl+C) | Banner "The engine isn't reachable" within 10 s; start it again → banner clears, data refreshes |
| 2 | Settings → General → Restart engine | "Restarting the engine…", then back at the same address |
| 3 | Settings → General | Version, data folder, config, pipelines folder, address, each with Copy; Show in folder opens the file manager |
| 4 | Pipelines folder → Browse… → choose another folder | Pipelines page lists it without a restart; `agentline.toml` has `pipelines_dir` |
| 5 | `kill -9` the engine, start it again | "agentline quit unexpectedly last time" banner; App reports lists it; View report marks it seen |
| 6 | App reports → Export | `agentline-report-….zip` downloads with `incidents/`, `system.txt`, `engine.log`, `summary.md` |
| 7 | Settings → General → Turn on notifications, start a run that reaches a gate, switch tab | One notification; title "(1) Agentline Studio"; Approve (Chrome) approves |
| 8 | Cmd/Ctrl+K, type "appr", Enter; then `g r` | Approvals opens; then Runs opens |
| 9 | Start a second `agentline serve` with the same `AGENTLINE_HOME` and another `--port` | It exits 1 naming the first engine |
| 10 | `AGENTLINE_API_TOKEN=x uv run agentline serve --host 0.0.0.0`, open from another machine | Sign-in screen; a wrong token is refused; `x` opens Studio; Sign out returns to it |
| 11 | Logs page | Says "engine log", offers Copy path |
| 12 | Mac app (`macos/scripts/build_app.sh --backend`) | Every native control behaves as before; no web banner, notifications or badges |

Automated:

```bash
uv run pytest tests/api/test_app_info.py tests/test_enginelock.py tests/test_runmarker.py -q
uv run pytest -m "not slow"
cd web && npm test && npx tsc --noEmit && npm run build
```
