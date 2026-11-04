# Phase 25: MCP servers managed and detected; the Chat can do everything the GUI can

Branch `phase-25`, merged into `main`.

## 1. MCP servers (Settings → MCP)

Engine:

- `[mcp.<name>] enabled = false` switches a server off without deleting it. A node that lists an
  off server runs without it (`compile._mcp_servers` leaves it out; the journal notes it as
  `mcp_disabled`). Validation is unchanged, so pipelines keep validating while a server is off.
- `GET /api/settings` gives each server `enabled` and `used_by: [{pipeline, node}]`
  (`mcp.used_by` scans the pipelines folder; pipelines that don't parse are skipped).
- `POST /api/mcp/test {name, body, secrets}` tests a server that isn't saved yet. `secrets` are
  key values typed into the popup. They are used only for this test (`test_server(..., extra=)`)
  and are not written anywhere.
- `GET /api/mcp/detect` and `POST /api/mcp/import {items: [{id, name?}]}` (`mcp_detect.py`) find
  servers in other tools' configs:

  | Tool | File |
  |---|---|
  | Claude Code | `~/.claude.json`: `mcpServers` (user) and `projects.<dir>.mcpServers` |
  | Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` (also `~/.config/Claude`, `%APPDATA%\Claude`) |
  | Codex | `$CODEX_HOME` or `~/.codex/config.toml`, `[mcp_servers.<name>]`, incl. `http_headers`, `env_http_headers`, `bearer_token_env_var` and `env_vars` |
  | Cursor | `~/.cursor/mcp.json` |
  | Project | `.mcp.json` in the pipelines folder, its parent and the working directory |

  Secret values never reach agentline.toml, the UI or the Chat. The following become `${KEY}`
  placeholders:
  - env vars whose name looks like a key (KEY, TOKEN, SECRET, PASSWORD, PAT, AUTH, …), or whose
    value is token-like;
  - every header (an `Authorization: Bearer x` header becomes `Bearer ${<SERVER>_TOKEN}`);
  - the argument after `--api-key`, `--token` and similar flags, plus secret-looking arguments;
  - secret query parameters in a URL.

  Existing `${VAR}` and `${VAR:-default}` references are kept as `${VAR}`. Only an import copies
  a value, from the source file to the secrets file. A key that is already set to a *different*
  value gets a server-specific name (`GITHUB_TOKEN_GITHUB`) and is never overwritten. A key
  already set to the same value is reused. Detection marks each server `new`, `configured` (the
  same command/args or URL is already in agentline.toml) or `problem` (can't be imported).

Web (`components/Mcp.tsx`, logic in `lib/mcp.ts`):

- The list shows each server with an On switch, Test, Edit and Delete, plus a "Used by"
  line (pipeline: nodes). Keys the server needs that aren't set appear with an amber status.
  Delete names the nodes that list the server.
- "+ Add server" opens a guided popup with four steps:
  1. Kind: local command, remote HTTP or remote SSE.
  2. Connection: name, command and arguments (quoting works), or URL.
  3. Keys and settings: env or header rows. Each row has a Secret tick and a key name; a
     secret's value goes to the secrets file and the table gets `${KEY}`. A plain value that
     looks like a secret blocks Next until it is ticked Secret.
  4. Test and save: test with the typed values, then save. Save writes the keys, then the table.

  Edit opens the same popup on step 2. Secret values show empty, which means keep the value
  that is already set.
- "Detect on this machine" lists the servers found, says which keys would move, and lets you
  rename the ones you tick before you import them.

## 2. The Chat can do what the GUI can

Every GUI action has a Chat action. The agent proposes it as a card. `assistant.check_actions`
checks the card against the config and **LIVE STATE**. LIVE STATE (`assistant.gather_live`)
covers the loaded pipelines, recent runs, waiting gates, issues, crash reports, the budget,
chat apps and notifications, the open pipeline's history, detected MCP servers and your own
skills. The prompt gets LIVE STATE so the agent proposes against real ids. A card that refers
to something that isn't there (a gate, an issue, a commit, a detected server, …) gets one
repair round, the same as a pipeline that doesn't validate (`REPAIRABLE`).

The web Chat applies each card through the same API as its GUI page (`lib/assistant.ts`
`applyAction`). "Apply all" skips cards that need their own click: anything that needs input
(a key value, a .zip), acts outside (a gate, a run, the engine, a download) or deletes.

| GUI | GUI action (API) | Chat action |
|---|---|---|
| Settings → Harnesses | add/edit harness (PUT /settings/harnesses/x) | `setting` section harnesses |
| | delete harness | `delete_setting` |
| | "Chat uses" (PUT /setup/choice) | `chat_uses` |
| Settings → Models (sources) | add/edit, delete (with its models after the confirm popup: `?with_models=true`) | `setting` / `delete_setting` section providers (`with_models: true`) |
| | sign Claude Code out of a subscription source (POST /providers/x/logout) | `claude_logout` |
| | choose the source's models (PUT /providers/x/models) | `source_models` |
| Settings → Models | add/edit, delete | `setting` / `delete_setting` section models |
| | hide / show in pickers (the eye: PUT /settings/models/x/visibility) | `model_visibility` |
| Settings → MCP | add/edit, on/off (`enabled`) | `setting` section mcp |
| | delete | `delete_setting` section mcp |
| | detect + import | `mcp_import` (ids from LIVE STATE) |
| | test | (read-only; the agent can ask you to press Test) |
| Settings → Skills | download/refresh library | `fetch_skills` |
| | new/edit own skill, delete | `skill` / `delete_skill` |
| | add skills from GitHub / skills.sh, remove one | `install_skills` / `delete_skill` |
| Settings → General → Other keys (and each feature's key field) | set a key | `key` (the value is typed on the card and goes only to /api/secrets) |
| | delete a key | `delete_key` |
| Settings → Chat apps | link (pair), send a test, unlink | `chat_app` op pair / test / unlink |
| | notifications | `chat_notify` |
| Settings → General | restart engine, choose pipelines folder (Mac app, or a browser: `path`), show the data / pipelines / config folder (phase 54) | `engine` op restart / choose_folder / reveal (`target`) |
| | sign out of the engine's API token, turn browser notifications on / off (phase 54) | none: sign-out removes this browser's own credential mid-conversation, and browsers allow the notification prompt only from a click |
| Pipelines | create, edit (save) | `pipeline` (full YAML + files; diff shown for edits) |
| | export / import | `export` (download) / `import` (the .zip is picked on the card) |
| | dry run | `dry_run` (the outcome is shown on the card) |
| | history restore | `restore` (commits from LIVE STATE HISTORY) |
| | delete a pipeline (phase 32) | `delete_pipeline` (its own folder too; history keeps it) |
| Settings | Chat uses > Images (phase 33) | `chat_vision` (`@auto`, `@claude-code` or a model name) |
| Runs | start a run | `start_run` (`text` for text / chat start points, phase 52) |
| | message a run's gatekeeper (Run view > Conversation, phase 52) | `run_message` |
| | retry a run / a node | `retry` |
| Approvals | approve / reject a gate | `gate` decision approve / reject |
| Problems | issue status, add a note | `issue` |
| | delete a crash report / all | `incident` op delete / clear |
| App reports | export every report as a .zip (phase 54) | `engine` op export_reports |
| | dismiss the "quit unexpectedly" banner (phase 54) | `incident` op seen |
| Costs | monthly budget | `budget` (null removes it) |
| Triage (phase 28) | triage a run / node (run view, Problems: Triage, Triage again) | `triage` |
| | add a lesson to the Chat instructions (triage card) | `lesson` |
| | report an infrastructure triage on GitHub (triage card) | `report_issue` |
| Run review (phase 38) | review a run (run view: Review, Review again) | `review` |
| | probe a review's pipeline edit (run view: Probe, Probe again) | `probe` |
| | add a lesson to the pipeline's lesson library (review card) | `pipeline_lesson` |
| Chat history (phase 48) | archive / unarchive a past conversation (Chat > History) | `conversation` op archive / unarchive (ids from LIVE STATE CONVERSATIONS) |

Read-only views (Logs, run journals, costs report, MCP and model tests) and Chat > History's
open / New chat / search (they only change what the panel shows) have no card: the agent
explains what it sees in LIVE STATE or asks you to look.

Chat links (Telegram, Slack, Discord) apply settings, skills, retries and pipelines as before.
For any other card type they answer "Apply this one in the app", and keys are still never set
over chat.

## 3. Settings → Skills

Each library skill's badge names the library it came from (`agent-skills`; the full
`owner/repo` is in its tooltip). Your own skills say "yours". Descriptions are clipped to one
line, with the full text on hover.

## 4. Chat instructions and skills (follow-up, branch `phase-27`)

- **Chat instructions** are the general instructions every Chat turn gets: in the panel and in
  chat apps. They live in one file per data folder, `$AGENTLINE_HOME/chat-instructions.md`
  (`chat_instructions.py`). The defaults are written on first use, and you can edit them
  afterwards. The defaults:

  > - When the user asks to build a new pipeline, invoke the `interview-me` skill first.

  Ways to change them:
  - API: `GET` / `PUT {text}` / `DELETE` (back to the defaults) on `/api/chat-instructions`;
  - Settings → Chat apps (the editor is open) and the "Instructions" toggle next to "Chat uses"
    on Agents. The editor lists the skills the text names in backticks and flags any that
    aren't in your skills;
  - a Chat `chat_instructions` action (the whole new text).

  The prompt puts the instructions right after the guide, under CHAT INSTRUCTIONS.
- **Skills in the Chat.** The prompt's SKILLS list marks your own skills and the built-in ones.
  - With claude-code, the Chat turn runs in `$AGENTLINE_HOME/assistant/workspace`
    (`assistant.ChatWorkspace`). Every skill is installed in its `.claude/skills/`, with your
    own first, then the fetched library, then the built-ins. Deleted skills are removed each
    turn. The allowed tools are `read` and `Skill`, so Claude Code invokes a skill itself when
    the instructions say to.
  - Other agents can't load skill folders. They get the full text of the skills the
    instructions name in backticks (`prompt_section`, as for pipeline nodes).
- **`interview-me` ships built in** (`src/agentline/builtin_skills/interview-me/SKILL.md`).
  It runs a short interview, one question at a time with a best guess and choices: goal,
  trigger, steps, AI or script, approvals, outputs. Then it summarizes and proposes the
  pipeline. Built-in skills have the lowest priority: the fetched library (which has its own
  `interview-me`) and your own skills win on a name clash. Settings → Skills shows them as
  "built-in".
- Chat links (Telegram, Slack, Discord) use the same `converse`, so they get the instructions
  and skills too.

Tests: `tests/ai/test_chat_instructions.py` covers:
- the defaults written on first use, edits, the size limit and reset;
- built-in versus library versus own skill priority;
- the action check;
- the API;
- claude-code getting the skills installed and the `Skill` tool, with deleted skills removed;
- an `llm` Chat getting `interview-me` written out in its prompt.

On the web, `assistantApply.test.ts` covers the `chat_instructions` card, the skill names the
editor finds, and the Save and Reset states. Checked in the browser: edit, save (the file is
written), the unknown-skill warning, Reset, and the Agents toggle.

## Tests

- `tests/ai/test_mcp_detect.py`: detection over fake Claude Code, Claude Desktop, Codex and
  `.mcp.json` files (secrets split out, conflicting keys renamed, no value in the output), the
  import API (secrets file and TOML contents), on/off, used_by, and the unsaved-server test.
- `tests/ai/test_assistant_parity.py`: every action type, valid and invalid against LIVE STATE,
  key values dropped, LIVE STATE text, and the chat API sending LIVE STATE plus the repair round.
- `web/src/lib/mcp.test.ts`, `web/src/lib/assistantApply.test.ts` (every action calls the right
  API; Apply all skips own-click cards), `SettingsExtra.test.ts` (skill rows).
- Checked in the browser against the dev server:
  - MCP list, used-by and the missing-key status;
  - the Edit popup: typing a secret, Test before saving, Save (the key lands in `secrets.env`
    and the TOML gets `${FAKE_T}`);
  - Detect and import from a `.mcp.json`;
  - Chat cards: Apply all for budget, dry run, MCP off and notifications, all confirmed on the
    server;
  - the Skills badges.
