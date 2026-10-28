# Phase 18: run tasks and journal, history, dry run, fix this, costs, own skills, more chats

## What the user asked
- Gap 5: MCP and Chat settings in the web UI. Gap 6: assistant Cancel, bounded chat state,
  `agentline import` without the server extra, settings applied from chat flag a restart.
- All suggested features: Fix this on Problems, a diff view for assistant proposals, dry runs,
  pipeline history and undo, a cost view with budget alerts, own skills, Slack and Discord.
- Run view: green done / red failed / yellow running / grey pending or waiting / greyed-out
  skipped; how many tasks run in each node (nodes can run several tasks); a journal per node
  and task, and restarting failed tasks (by the user or the main agent).
- Test whether the assistant can build the pipeline in an image (spec-writer -> builder -> 4
  parallel reviewers -> wait for all -> rework max 2 rounds -> approver -> merge or human).
- Gaps 1–4 (real runs against services, the Keychain prompt after rebuilds, Codex MCP secrets
  on argv, clicking through the Mac screens) are the user's to fix by hand.

## As built
**Tasks and journal** — `for_each: plan.steps` + `max_parallel` on agent/action nodes; tasks in
the `tasks` table, every status change / task event / retry in `journal` (SQLite + Postgres,
contract-tested). `POST /api/runs/{id}/retry {node?}`, `GET /api/runs/{id}/journal`,
`agentline runs retry RUN --node N`, Telegram `/retry` + a Retry button, assistant `retry`
action. Found and fixed: a manual retry of `shell.run` reused the recorded failure.

**Run view** (web and Mac) — the color scheme above, task chips (`▶2 ✓5 ✗1 /8`), a toolbar
summary, a node panel with Tasks, Journal and Retry failed tasks.

**The diagram** — no real `claude` here, so the assistant can't be run for real. Instead:
image attachments for the assistant (Claude Code reads them with its Read tool); the pipeline
built as the assistant should (`src/agentline/templates/feature-factory`), validated, and dry
run in three scenarios (changes once then approve; changes 3x -> needs-review after 2 rounds;
escalate -> human gate -> merge). The dry run found two real bugs (optional input outside its
block, now a validation rule; a regex shadowing in the template module). `github.merge_pr`
added. The assistant's guide and reference now cover joins, for_each and merges.

**Dry run** — `POST /api/dryrun` and an editor button: stubbed effects, scripted routers,
auto-approved gates, per-node result and journal.

**History** — `.agentline-history` (a private git dir in the pipelines folder); `/api/history`,
`/history/show`, `/history/restore`; a History dialog with a diff. Assistant proposals that
update a pipeline show a diff.

**Fix this** — `fixit.py`; `/api/issues/{n}/fix`, `/api/runs/{id}/fix`; web `#/fix/...`
routes hand the request to the Assistant; Mac "Fix This…"; Telegram "Fix it".

**Costs** — `costs.py`; `/api/costs`, `/api/budget`; web Costs page; Mac Runs > Costs; chat
alerts at 50/80/100% of the monthly budget.

**Own skills** — `$AGENTLINE_HOME/skills/local`; `/api/skills/{name}`; Settings > Skills.

**Slack and Discord** — `chatlink/ws.py`, `sockets.py`, `slack.py`, `discord.py`,
`platforms.py`; tested against a local websocket server and fake HTTP APIs, not real
workspaces.

## Not done / follow-ups
- Real Slack/Discord/Telegram runs; WeChat.
- The assistant has no streaming, only a timer and Cancel.
