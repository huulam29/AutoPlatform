# CLAUDE.md

Context for future sessions working on agentline. Keep this current with every phase.

## What this is
Self-hosted engine for durable multi-agent delivery pipelines (YAML graph of agent / action /
gate / router / wait nodes). Design, data model, state machines and the decision log live in
[ARCHITECTURE.md](ARCHITECTURE.md) — read it before changing engine behavior, and add to its
decision log (§12) when the user decides something.

## Status
- Phase 0 (design + failure demo): done, merged to `main`.
- Phase 1 (durable core): done. `demo.chaos --target durable` is clean over 50 jobs.
- Phase 2 (YAML pipeline engine): done. `demo.chaos --target pipeline` runs the full
  ticket-to-eod graph clean over 50 tickets with one review loop each.
- Phases 3–5 (runners/workspaces, actions/webhooks/poller, artifacts/lineage): done, built in
  parallel worktrees and merged. As-built notes: `docs/phase-3.md`, `docs/phase-4.md`,
  `docs/phase-5.md`.
- Phases 6–7 (CLI/audit/logs/redaction, visual editor + runs API): done, merged.
  `docs/phase-6.md`, `docs/phase-7.md`.
- Phase 8 (Postgres ledger, Kafka source, codex/aider runners, schedules, templates): merged.
  Postgres is untested against a real server (contract tests skip without `AGENTLINE_PG_DSN`).
- Phase 9 (end-to-end acceptance): `python -m demo.e2e.run --tickets 10 --expect-clean` is clean
  under SIGKILL chaos. `docs/phase-9.md`.
- Phase 10: real-run kit (`scripts/real_run.sh`, `examples/real-run/`) dry-runs clean with
  `scripts/dry_claude.py`. **The real Claude Code run is still to do** (no `claude` CLI on the
  build machine); record it in `docs/real-run.md`.
- Phase 11 (agents vs model sources): backend done on `phase-11` (providers/models/agents
  config, `agent:`/`model:` per node, `defaults:`, LLM routers, `llm` runner, secrets file,
  Settings API) and GUI (Settings page, pickers, defaults, LLM routers): merged.
  Guide: `docs/harnesses-and-models.md`; GUI notes: `docs/phase-11-gui.md`.
- Phase 12 (native Mac app): engine side on `phase-12` (approvals/start/status API, Jira poll
  trigger, Keychain-managed keys, parent-pid watchdog, `macos/scripts/build_backend.sh`) and
  the SwiftUI app in `macos/` (`macos/scripts/build_app.sh --backend [--install]`; `swift test`
  in `macos/`): merged. `docs/phase-12.md`, `docs/phase-12-mac.md`, `macos/README.md`.
- Phase 13 (crash/failure reports): engine (`incidents.py`, hooks, /api/incidents,
  `agentline incidents`) and Mac app (Problems screen, crash/unclean-exit/engine-exit capture,
  export): merged. `docs/phase-13.md`, `docs/phase-13-mac.md`.
  **When asked to fix reported problems: `agentline incidents list/show`, `agentline triage
  list/show`, or read the exported folder / `~/Library/Application Support/agentline/incidents/*.json`.**
- Phase 14 (UI + defaults): editor `+` add-node menu, collapsible panels, growing minimap
  (`docs/phase-14-web.md`); built-in sources/models/agents (only keys needed); keys always
  settable (secrets file only since phase 22); validator fix for extra unconditional edges: merged.
- Phase 15 (Mac fixes): sidebar rows switch the detail page; one app per data folder
  (`InstanceLock`, `flock` on `<home>/app.lock`; a second copy hands over and exits): merged.
- Phase 16 (assistant, skills, issues, settings help): editor assistant chat
  (`assistant.py`, `api/assistant.py`, `web/src/components/Assistant.tsx`), agent skills
  (`skills.py`, `skills:` on agent nodes), Problems issues (`issues.py`, `api/issues.py`,
  `agentline issues`), model-source fix-it sheet + Models checklist + Models search/filter/sort
  (Mac). `docs/phase-16.md`. The assistant was tested with a stand-in `claude` only.
- Phase 17 (setup, MCP, import/export, chat links): first-run "Connect a model" (`setup.py`),
  MCP servers (`mcp.py`, `mcp:` on agent nodes), pipeline bundles (`bundle.py`,
  `agentline export/import`), Telegram chat link (`chatlink/`, run by `serve`; `chat.ask`,
  `chat.send`), node-type guidance + script steps for the assistant. `docs/phase-17.md`.
  Telegram and MCP are tested against fakes only.
- Phase 18 (runs, history, dry run, costs, skills, more chat apps): `for_each` tasks + `tasks`
  / `journal` tables, node retry, run view colors/counts/journal, pipeline history (`history.py`),
  diff view, dry run (`dryrun.py`), feature-factory template (the user's diagram), Fix this
  (`fixit.py`), costs + budget (`costs.py`), own skills, Slack/Discord links (`chatlink/ws.py`,
  `slack.py`, `discord.py`), assistant image attachments, `github.merge_pr`. `docs/phase-18.md`.
- Phase 19: Claude subscription source with no `claude` CLI → Test fails with a fix-it popup.
- Phase 20: the web UI is **Agentline Studio**, the Mac app's only window (native screens
  retired; `NativeBridge.swift` answers Keychain/logs/Finder/engine requests from
  `web/src/lib/native.ts`). New web Approvals/Problems/Logs/General pages, "Chat uses" bar,
  Assistant renamed Chat, `GET /api/logs`. `docs/phase-20.md`.
- Phase 21 (Flight Deck redesign): `web/src/theme.css` token source, restyled `styles.css`,
  `components/Icon.tsx` (icons, `<Status>`), status tones incl. amber "attention".
  `docs/phase-21.md`, `docs/redesign-proposal.md`.
- Phase 22: Mac keys moved from the Keychain to `<home>/secrets.env` only (no prompt after
  rebuilds); one-time `KeyMigration` on first launch per data folder.
- Phase 23: "Chat uses" lists only the chosen agent's models (`compatible_models`); the Chat is
  global (any page), opened by the top-bar Chat button at the far right or Cmd/Ctrl+J.
- Phase 25: MCP servers on/off (`enabled = false`), used-by, guided add/edit popup with secret
  fields and a test before saving, detect + import from Claude Code/Desktop, Codex, Cursor and
  `.mcp.json` (`mcp_detect.py`; secrets to the secrets file only). Chat parity: an action for
  every GUI action, checked against LIVE STATE (`assistant.gather_live`), applied by the web
  through the GUI pages' APIs (`lib/assistant.ts applyAction`). `docs/phase-25.md` has the table.
- Phase 27: Chat instructions (`chat_instructions.py`, `/api/chat-instructions`, Settings → Chat
  apps) and skills in the Chat (claude-code: `<home>/assistant/workspace/.claude/skills` + `Skill`
  tool); built-in skills in `builtin_skills/` (`interview-me`). See docs/phase-25.md §4.
- Phase 32: delete a pipeline (API, editor button, Chat `delete_pipeline`); one folder per
  pipeline (`agentline new` writes `<name>/pipeline.yaml`); `pipeline/files.py` is the one rule
  for which files are pipelines (serve used to skip subfolders); Chat rule FRESH_DESIGN.
  `docs/phase-32.md`.
- Phase 33: Chat images. `toHistory` kept dropping attachments (fixed); images are staged in
  Claude Code's folder; other agents get a vision transcript (`vision.py`, Chat uses > Images,
  transcript card with correct/redo, cached per image). `docs/phase-33.md`.
- Phase 35: **Agents → Harnesses**, `llm` → `builtin` (`[harnesses.*]`, `harness:`, Settings →
  Harnesses). Only the harness meaning: node `type: agent`, runner names and router `mode: llm`
  stay. Old names are read everywhere (aliases in `config.py`, `agents.harness_name`,
  `spec.OLD_FIELDS`, `roundtrip._respell`, `tomlwrite.migrate_section`). `docs/phase-35.md`.
- Phase 36: built-in Chat harness `chat_harness.py` on Pydantic AI (`pydantic-ai-slim`, `server`
  extra): lookups, typed reply checked + retried once, App reports (`chat-harness` incidents) for
  gaps/loops/invalid/unparseable/unknown/crashes. First-run setup in two steps (model API, then
  harness) in `SetupDialog.tsx`, also in the Mac app (native `SetupSheet` removed).
  `docs/phase-36.md`. Not tried against a real provider yet.
- Phase 37: Settings opens on Model sources (before Harnesses). Removing a source asks in a popup
  listing its models and removes them too (`DELETE /settings/providers/x?with_models=true`);
  subscription sources show the Claude Code login with Sign out (`/providers/x/login|logout`,
  Chat `claude_logout`). Built-in sources can be removed (`[removed] providers`, their built-in
  models go too; a preset brings them back). `readiness.py`: a source is ready when its key is
  set or Claude Code is logged in (`claude auth status`, cached 30 s); Test fails when signed
  out, and the Models tab + pickers (`compatible_models`) hide models of sources not ready.
- Phase 38 (run review and harness learning, ideas from Google's Harness²): finished runs with
  signals (loops, rejections, retries, needs-review, empty output, cost/time spikes) are reviewed
  by the Chat's harness (`review.py`, loop in `serve`; `AGENTLINE_REVIEW=off` in tests): rows
  with verdicts, edit cards, one lesson for the pipeline's `lessons.md` (`lessons.py`). Probes
  replay one agent node with an edit on its recorded inputs (`probe.py`; coding nodes in a
  remote-less clone at `start_sha`, MCP off unless `probe_safe`). A review's edit is applied by
  the server (`/reviews/{id}/apply`, keeps the old files for an undo) and checked on the next
  runs (helped / hurt / no effect). Run view Review panel, editor Lessons tab, `agentline
  review|lessons`, Chat `review`/`probe`/`pipeline_lesson`. `docs/phase-38.md`. Not tried
  against a real model yet.
- Phase 39 (phase 38 gaps): editor Lessons tab (Graph | YAML | Lessons) instead of the Learning
  dialog; review/probe costs use `add_cost(on_execution=False)` (run total, not the step's);
  applied lesson cards remembered (`lesson_added`); Settings → MCP "Safe for probes"
  (`probe_safe`). Notes in `docs/phase-38.md`.
- Phase 40: **Lessons** page in the top bar (`#/lessons`, `GET /api/lessons`): every pipeline's
  lessons and adopted edits; the editor tab stays.
- Phase 41: Model sources → Test / list models is a checklist; "Add N models" adds them all
  (`PUT /providers/{name}/models` with `add_only`). `docs/phase-41.md`.
- Phase 42: live reload (`live.py`): settings and pipeline saves reach the running engine
  without a restart; settings changes apply to runs already going, structure changes to new
  runs only (`App.graph(flow, run)` by `flow_hash`). `docs/phase-42.md`.
- Phase 43: Chat panel has Harness + Model pickers; models = `compatible_models[harness]`
  (kind fits and source ready). `docs/phase-43.md`.
- Phase 44: only `builtin` is a built-in harness (and the fallback); claude-code / codex / aider
  are presets added by the user; pickers list configured harnesses + "+ Add a harness…";
  one-time `harness_migration.py`. `docs/phase-44.md`.
- Phase 45: Settings saves are silent (no "no restart needed" banner).
- Phase 46 (Settings: only what is usable): one **Models** page (`components/settings/
  ModelsPage.tsx`, `AddModelsDialog.tsx`, `lib/models.ts`): groups per connected source,
  Needs attention, Add models (source → connect → choose), eye = `hidden` (PUT
  `/settings/models/x/visibility`, Chat `model_visibility`). No Model sources / Keys tabs: keys
  live with their feature, the rest in General → Other keys (`KeyStatus.owner`). Add harness
  lists installed programs, missing ones collapsed with install commands. Built-in Ollama is
  ready only while it answers (`readiness.server_up`). `docs/phase-46.md`.
- Phase 47: first-run setup step 1 is the Add models steps inline (`AddModelsFlow embedded`
  in `SetupDialog.tsx`) under a list of connected sources; a custom endpoint whose test failed
  can be retried under the same name. `docs/phase-47.md`.
- Phase 48: Chat history. Conversations live on the server (`conversations.py`, one JSON file
  per conversation in `<home>/assistant/conversations/`, `/api/assistant/conversations`), not
  localStorage; Chat panel History view (`components/ChatHistory.tsx`, `lib/conversations.ts`)
  with search and an Archived section (no activity for 7 days; Unarchive; never deleted);
  Chat action `conversation` archive/unarchive. `docs/phase-48.md`.
- Phase 49: OpenCode and Hermes Agent harness presets (`opencode`, `hermes-agent` of kind
  `hermes`): runners `runners/opencode.py` (`opencode run --format json`, permissions through
  `OPENCODE_PERMISSION`) and `runners/hermes.py` (`hermes chat --query= --oneshot --format
  stream-json`, toolsets from `allowed_tools`); install commands in Add harness. Tested
  against fake CLIs only. No MCP for either. `docs/phase-49.md`.
- Phase 51: built-in Chat harness: OpenRouter `finish_reason: "error"` is a user-side error (not
  reported); output retries 2; a reply that reached the checks is kept (as `invalid`) when the
  retry never parses. `docs/phase-51.md`.
- Phase 52 (start point + chat gatekeeper): `start: {node, input: json|text, template}`
  (`pipeline/start.py`; Start run popup pre-fills the JSON template or takes text), node type
  `chat` (`runchat.py`): resumable waits (`ctx.wait(..., resume=True)`, WAITING → READY), the
  run thread (`run_messages` table, `/api/runs/{id}/messages`), `RunChatLoop` in `serve`
  (`AGENTLINE_RUNCHAT=off` in tests) for notes, answers and Confirm cards; Run view
  Conversation (`components/RunChat.tsx`), editor Start point; chat apps forward the thread
  (`/run <pipeline> <words>`, reply or `/say`, card buttons); Chat `run_message`; `helpdesk`
  template. `docs/phase-52.md`. Tested with fakes only.
- Phase 26: Settings pages (Agents, Model sources, Models, MCP, Skills) have no side panel: a
  page header plus rows that open in place (`components/SettingsRows.tsx`). `docs/phase-26.md`.
- Phase 28 (failure triage): `triage.py` classifies each failed run / node (auto, in `serve`)
  as ai-instruction / infrastructure / external with the Chat's agent; proposals are cards
  (pipeline edit, `lesson` for the Chat instructions, retry); infrastructure reports go to the
  run's issue and optionally GitHub (`[report] github_repo`). Run view, Problems, Chat card,
  chat links, `agentline triage`. `docs/phase-28.md`.
- Phase 29: Problems = user-fixable only; crash reports and infrastructure verdicts are App
  reports (`#/app-reports`, `/api/app-reports`, `triage.triage_incidents`: no model call) with
  GitHub reporting; Chat instruction rule via `chat_instructions.UPGRADES`. `docs/phase-29.md`.
- Phase 31: GitHub setup in Settings → General (`github_setup.py`, `/api/github`,
  `GitHubSetup.tsx`): GitHub CLI browser sign-in (`gh auth login --web`) or a pasted token, repo
  from detected SSH/HTTPS remotes, written to `[report]` (`tomlwrite.set_plain_table`); Chat
  `github` action. `docs/phase-31.md`.

## Commands
```bash
uv sync                                    # .venv + dev deps (pytest, ruff)
uv run pytest                              # all tests (~15 s; chaos ones are marked slow)
uv run pytest -m "not slow"                # fast subset
uv run ruff check . && uv run ruff format --check .
git config core.hooksPath .githooks         # once per clone: pre-commit refuses secrets, formats staged files + ruff check
uv run python -m demo.chaos --jobs 50      # failure demo; --expect-clean for acceptance
uv run python -m demo.chaos --target pipeline --jobs 50 --expect-clean
uv run agentline validate examples/ticket-to-eod/pipeline.yaml
uv sync --extra server && uv run agentline serve      # webhooks + editor + runs API on :8080
cd web && npm ci && npm test && npx tsc --noEmit && npm run build   # editor (served from web/dist)
macos/scripts/build_app.sh --backend          # Mac app -> macos/build/Agentline.app (--install)
(cd macos && swift test)                      # Mac app tests
uv run python -m demo.e2e.run --tickets 10 --seed 1 --expect-clean   # v0.1 acceptance (server extra)
uv run python -m demo.chaos --target kafka --jobs 50 --expect-clean
AGENTLINE_PG_DSN=postgresql://... uv run pytest tests/ledger -rs     # Postgres contract tests
uv run python -m agentline.api.dev --pipelines examples --db /tmp/x.db --port 8787   # dev server
```

## Layout
- `src/agentline/` — the engine. Stdlib + sqlite3 only.
  - `states.py` enums + allowed transitions; `graph.py` NodeDef/EdgeDef/Graph + validation.
  - `ledger/` storage protocol (`tx()` + row ops) and `SqliteLedger`.
  - `scheduler.py` every state change as a function over one tx (submit, complete, fail,
    park, reclaim, deliver_event, resolve, cancel, retry_run). Fenced by `Fence(status, epoch)`.
  - `context.py` StepContext: `effect()`, `spend()`, `check_budget()`, `wait()`, `route()`.
  - `worker.py` leases/heartbeats/reaper; `app.py` App + Flow (`@flow.step`).
  - `registry.py` extension interfaces (Runner, Action, Waiter, WorkspaceProvider) + Registry;
    built-ins register in `runners/`, `actions/`, `workspaces/` `__init__.register()`;
    `builtins.default_registry()` collects them. `config.py` loads `agentline.toml`.
  - `pipeline/` YAML spec → validate → compile to Graph.
  - `runners/` python, anthropic (urllib), shell, claude-code; `workspaces/` git mirrors,
    local + container providers, image resolution/proposals.
  - `actions/` jira.*, github.*, shell.run, env.deploy/test, github.review waiter;
    `integrations/` stdlib HTTP clients; `api/webhooks.py` FastAPI (optional `server` extra);
    `poller.py` fallback for missed webhooks; `deliveries.py` replay/dedupe table.
  - `artifacts.py` content-addressed store, summaries, lineage; large inputs as references.
  - `agents.py` agent kinds + agent/model resolution; `runners/llm.py` direct model calls
    (OpenAI-compatible or Anthropic); `secrets.py` local secrets file; `tomlwrite.py` safe
    table edits of agentline.toml; `api/settings.py` Settings API.
  - `cli/` the `agentline` command; `logs.py` JSON logs; `redact.py` secret redaction;
    `incidents.py` local crash/failure reports (API: `api/incidents.py`).
  - `pipeline/roundtrip.py` comment-preserving YAML edits for the editor; `api/editor.py`,
    `api/runs.py`, `api/server.py` (`create_server`: webhooks + editor + runs + static UI).
- `web/` React + TypeScript + React Flow editor and run view (Vite; vitest).
  - `ledger/postgres.py` + `ledger_from_url`; `sources/` Kafka source + FakeBroker;
    `schedules.py` cron + schedule loop; `templates/` packaged pipeline templates;
    runners `codex`, `aider`.
- `demo/e2e/` v0.1 acceptance harness (fake Jira/GitHub process, scripted agent, EOD scripts).
- `examples/ticket-to-eod/` the target pipeline (prompts, plan schema); `examples/agentline.toml`.
- `demo/durable/worker.py` — the chaos job on agentline; `demo/pipeline/worker.py` — the full
  example pipeline with fakes backed by `demo/fakes/server.py`.
- `demo/fakes/server.py` — fake GitHub + LLM HTTP server, SQLite-backed counts.
- `demo/naive/` — the deliberately broken at-least-once queue + worker.
- `demo/chaos.py` — chaos harness; add new engines as entries in `TARGETS`
  (implement `submit`, `worker_cmd`, `worker_env`, `done_tickets`).
- `tests/` mirrors the source tree.

## Key decisions (details in ARCHITECTURE.md §12)
- Explicit per-node state machine, **not** replayed handlers. A step runs once per claim; its
  result + successor transitions commit in one fenced SQLite transaction.
- `on_unknown`: `halt` default, `retry`, `ai` (advisor; never for deploy-class effects).
- Pipeline spec v1: `version`, `type: wait`, router `switch:`, structured conditional edges,
  `env.deploy`/`env.test` from `agentline.toml`. Deploy-class nodes must be gated.
- Workspaces clone from a local bare mirror; approvals CLI-only in v0.1.
- One generic executor; nodes are pure config. Code is per runner / per action, never per node.
- Harnesses (claude-code/codex/aider/builtin; "agents" before phase 35), model sources and named
  models are separate config; nodes pick `harness:`/`model:`, `defaults:` are a pair. Claude Code: Anthropic models only.
  Keys live in env vars or `~/.agentline/secrets.env`, never in agentline.toml.
  Agents take `prompt` (file or inline, `{{artifact.field}}` templating) and `output_schema`.
- Container images (§11.1): explicit / repo profile / deterministic `auto`, layered and cached
  by content hash. A missing tool sends the node to needs-review, is shown in `runs show`, and
  `image.propose` drafts the image change automatically; built only after `agentline approve`, written to local `agentline.toml` only.

## Conventions
- Python 3.11+, asyncio, type hints everywhere. No agent frameworks. FastAPI only from Phase 4.
- Small commits, tests for everything. Tests use fakes for Jira, GitHub and LLMs.
- Never real credentials in code, tests or the ledger; env vars only.
- Git: each phase on a branch `phase-N`, merged
  into `main` with `--no-ff` when done (user: "each branch per phase that merge to main"). Commits end with the Co-Authored-By trailer.
- Work phase by phase; plan first, ask on non-obvious design decisions. Parallel phases run as
  subagents in worktrees; each writes `docs/phase-N.md` and never edits CLAUDE.md/ARCHITECTURE.md
  (the integrator folds them in on merge).

## Gotchas
- Mac app UI is the web UI: add screens in `web/`, not SwiftUI. Anything only the app can do
  goes through a `NativeBridge` action (reply objects; `{ok: false, error}` on failure) and is
  hidden in a browser (`isMacApp()`). `engine.restart` replies before restarting.
- SwiftUI macOS sidebar (no longer used by the app): `ForEach(items, id: \.self)` + `.tag(item)` rows + a non-optional
  `List(selection:)` binding of the same type. `ForEach` over `Identifiable` tags rows with `id`
  (no match: nothing selects), and `NavigationLink(value:)` rows don't switch the detail column.
  Check by clicking in the built app, not just `swift build`.
- Verification that writes reports, crashes or data (`AGENTLINE_TEST_CRASH`, snapshots) runs on a
  throwaway data folder (`AGENTLINE_APP_SUPPORT_OVERRIDE=<scratch dir>`), never the user's real
  one; the test-crash hook refuses to fire otherwise (phase 30).
- Mac app: a second copy must `exit(0)` when handing over, not `NSApp.terminate` (that would
  stop nothing but remove the first copy's run marker).
- Subagent worktrees live in `.claude/worktrees/` (git- and ruff-ignored).
- Smoke tests (real `claude`, real docker) run only with `AGENTLINE_SMOKE=1`; see `docs/smoke.md`.
- Test fake HTTP servers: use the `_Server` getfqdn trick and `serve_forever(0.02)`.
- Commit chains: `set -o pipefail` before `pytest ... | tail -1 && git commit` (without it a
  failing suite still commits). Never commit from a background job.
- Effects named by attempt (`shell.run`'s `run#…`) use `ctx.attempt_key`, not `ctx.attempt`:
  a manual retry resets the attempt to 1 and would reuse the recorded result.
- `dryrun.py` replaces router handlers on the compiled graph (`node.handler = ...`); keep
  NodeDef mutable.
- Chat link tests: `tests/ai/fake_ws.py` (websocket) and fake HTTP APIs for Slack/Discord;
  `tests/ai/fake_telegram.py` (set `TELEGRAM_BOT_TOKEN` and
  `AGENTLINE_TELEGRAM_API`); drive `ChatLink.step()` directly with `poll_wait=0`.
- Every test gets a temp `AGENTLINE_HOME` (autouse in `tests/conftest.py`), so the real
  `~/.agentline` is never touched; `App()` loads `$AGENTLINE_HOME/secrets.env`.
- The repo path may contain spaces: shell-quote paths in generated scripts and command strings.
- Tests are sync and call `asyncio.run(...)` (no pytest-asyncio dependency).
- `ruff check --fix` can delete `except ... as e` bindings used only inside lambdas; bind to a
  local (`msg = str(e)`) before the lambda.
- `Worker.run_until_idle()` waits until no execution is ready/running/failed in the whole
  ledger, so it also waits out a dead worker's lease.
- `http.server.HTTPServer.server_bind` calls `socket.getfqdn()`, which takes ~35 s on this Mac.
  `demo/fakes/server.py::_Server` skips it; do the same for any stdlib HTTP server.
- ruff formats Python code blocks inside Markdown files too.
- Web: an inspector field must not be a `<label>` wrapping buttons (a click on its help text
  clicks the first button: remove). Effects need block bodies when the call returns something
  (`scrollIntoView()` returns a Promise in new Chromium; React calls it as the cleanup).
  Prettier isn't used here: don't run it (it reformats whole files).
- Web styling: values come from `web/src/theme.css` only (semantic tokens); check with
  `python3 <ux-ui kit>/scripts/lint_hardcodes.py web/src`. Status shows icon + word via
  `<Status>`; node kinds are icon + label, not colors.
- Harness → model pickers: every Model select that sits next to a harness choice (node inspector,
  pipeline defaults, "Chat uses", setup) lists only the models that agent supports, from the
  server's `compatible_models[agent]` (`agents.summary`; claude-code = Anthropic models only).
  Never list all `[models]`. On an agent switch, drop a model the new agent can't use
  (`lib/ai.ts` `chatModelOptions` / `modelAfterAgentChange`).
- Chat messages to the server go through `toHistory` (`web/src/lib/assistant.ts`): a field
  added to `ChatMessage` that the server needs (attachments) must be kept there too.
- Live views of a trimmed buffer (Logs, journals, anything polled that drops old lines): with
  Follow off, render a frozen snapshot and show "N new lines" instead of re-rendering; the
  top trim otherwise pushes the text the user is reading upward. Scrolling up turns Follow off
  (`components/Logs.tsx`, `lib/logs.ts newSince`).
- Access logs: successful GET/HEAD requests are dropped (`logs.QuietAccessFilter` on
  `uvicorn.access`) because the app and the UI poll every few seconds; errors and writes stay.
  `AGENTLINE_ACCESS_LOG=all` brings them back for debugging. Keep it that way.
- Built-in harness tests: `tests/ai/fakes.py` `FakeLLM` answers with `tool_calls(...)`; real
  OpenAI-compatible servers must get schema-complete responses (the SDK validates `object`,
  `index`, `total_tokens`). `uv sync` without `--extra server` drops uvicorn (dev server fails).
- Tests: `tests/conftest.py` keeps claude-code / codex / aider set up and the old claude-code
  fallback for older tests; mark a test `@pytest.mark.builtin_only` to get the real behavior.
- Code that looks a run's graph up passes the run: `app.graph(run.flow, run)` (scheduler, worker,
  review), so a run keeps the structure it started with across live reloads.
- `agentline serve` runs automatic failure triage and run reviews, which call the real Chat
  agent: `AGENTLINE_TRIAGE=off` and `AGENTLINE_REVIEW=off` (set by `tests/conftest.py` and
  demo/e2e) keep tests from doing so.
- Lineage `read` links record every execution's inputs per iteration (`list_artifact_links`):
  use them to replay a node (probes), not `resolve_inputs` (that's the latest outputs).
- Triage costs go through `add_cost(..., touch=False)`: a cost recorded after a run ended must
  not bump `runs.updated_at` (issues.sync would count the problem as happening again).
- Chat parity: a new GUI action needs a Chat action too: `ACTION_TYPES` + `_check_one` (and
  LIVE STATE if it refers to live ids) in `assistant.py`, the GUIDE list, `applyAction` /
  `actionTitle` in `web/src/lib/assistant.ts`, and a row in docs/phase-25.md's table.
- Web: don't spread DOM events (`{...e}` copies none of `key`/`metaKey`: they're prototype
  getters); read the fields (`web/src/lib/ui.ts` `keyLike`).
- Workers are configured via env vars (see `demo/naive/worker.py` docstring) so the chaos
  harness can run any worker the same way.
