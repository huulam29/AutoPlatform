# Phase 16: editor assistant, agent skills, issues, model-source help

## What the user asked
1. A chat in the Pipeline Editor that uses one of the configured agents (claude-code by
   default), interviews the user to set up only what's needed (agents, model sources, models,
   keys), and builds or edits whole pipelines, pre-loading skills from
   addyosmani/agent-skills per node.
2. Problems: an issue for each problem, with status tracking.
3. Model sources: a failed Test opens a window that helps fix it; a Models button with a
   checklist of the source's models (none imported by default).
4. "Load the source's models" failed.
5. Models tab: filter, sort, search.
Also fixed during the phase: clicking a field's help text in the inspector removed the field.

## Decisions (user)
- Issues: local tracker only (ARCHITECTURE §12 #39).
- Skills: fetched from GitHub on demand (#40).
- Assistant: show proposals, apply on click (#41).

## As built
**Assistant** (`src/agentline/assistant.py`, `api/assistant.py`, `web/src/components/Assistant.tsx`,
`web/src/lib/assistant.ts`)
- `POST /api/assistant/chat` resolves the chosen agent like a node does and runs it through its
  runner with `allowed_tools: [read]`, `max_turns: 4`, 300 s. The prompt carries the guide, the
  settings (key status only), agent CLI readiness, actions/waiters/workspaces, the spec rules,
  the skill list and per-step suggestions, the pipelines on disk, the ticket-to-eod template and
  the open pipeline (unsaved edits included). The reply is JSON `{message, choices, actions}`.
- Actions are checked server-side: settings with the same parsers as agentline.toml, pipelines
  validated in a scratch copy of their folder with their files. A pipeline with issues is sent
  back once for repair. Nothing is written until `POST /api/assistant/apply` (pipelines) or the
  existing settings / secrets / skills endpoints are called by the panel.
- The panel lives in the pipelines view (right rail, collapsed by default), keeps the chat in
  localStorage (no key values), and tells the agent which proposals were applied.
- Verified in the browser against a stand-in `claude` script (no real `claude` CLI on the build
  machine): proposals, quick replies, key entry, Apply all, pipeline creation and navigation.
  Real model behaviour is untested.

**Skills** (`src/agentline/skills.py`, `api/skills.py`, `agentline skills list|fetch|show`)
- `SkillLibrary` downloads the GitHub tarball and keeps only `skills/**` and `LICENSE` (regular
  files only), swapped in atomically. 25 skills as of 2026-10-19.
- `skills:` on agent nodes; validated against the library once fetched; at run time a missing
  library or skill fails the node (permanent). claude-code + workspace: `.claude/skills/<name>`
  (added to the checkout's `info/exclude`); otherwise appended to the prompt.

**Issues** (`src/agentline/issues.py`, `api/issues.py`, `agentline issues list|show|set|note`)
- `GET /api/issues` syncs first: every incident and dead / needs-review run gets an issue;
  a newer occurrence of a `fixed` issue reopens it. `PATCH` changes status / adds notes.
- Mac Problems screen: Open / Closed / All filter, `#N status` tags, an issue panel (status,
  note, history) above each problem, "Other issues" for issues whose problem is gone.

**Settings (Mac)** (`SourceSheets.swift`, `SettingsLogic.swift`, `SettingsScreen.swift`)
- Item 4 cause: a new model defaulted to the built-in `claude` subscription source, whose
  model listing always errored. Subscription sources now list the known Claude Code models, and
  a new model defaults to a source with a key set.
- `/api/providers/{name}/test` failures carry `problem` (no_key, auth, unreachable, http,
  bad_response) and `key`; the help sheet sets the key (Keychain, then restarts the engine and
  retests) or saves a fixed URL and retests.
- `PUT /api/providers/{name}/models {models: [ids]}` makes the checked set real (derived names;
  built-in and agent-default models are kept).
- Models tab: search, source filter, built-in/custom filter, sortable columns.

## Not done / follow-ups
- The Mac UI changes were built and unit-tested (`swift test`), not clicked through in the built
  app (the user's copy was running; a second copy hands over to it).
- A real `claude` run of the assistant.
