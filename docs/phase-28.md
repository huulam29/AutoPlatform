# Phase 28: automatic failure triage

When a pipeline run goes wrong, agentline works out whose fault it is and proposes what to change:

- **ai-instruction**: the pipeline's own instructions led the agent (or a script step) wrong.
  Triage proposes an edit to the node's prompt / schema / settings, and a **lesson**: one line
  for the Chat instructions, so pipelines built later don't make the same mistake.
- **infrastructure**: agentline itself misbehaved. Triage writes a redacted report, attaches
  it to the run's local issue (Problems) and, when GitHub reporting is set up, offers a card to
  open a GitHub issue. Without it there's a "Copy as GitHub issue" button.
- **external**: a third-party outage, a missing or revoked key, a CLI that isn't installed.
  Triage proposes the keys / settings to set.

Every verdict also proposes a **retry** of the failed node, the same as "Fix this".

## The user's decisions (decision 63)

| Question | Answer |
|---|---|
| Run automatically or on click? | **Automatically** on every failure; Triage / Triage again buttons too |
| Apply fixes and retries on their own? | **No: proposal cards** the user applies (as decisions 16 and 41) |
| Count triage cost against the pipeline's budget? | **Yes, against the run's `budget_usd`**; triage is skipped while that budget is used up |

## How it works

`src/agentline/triage.py`:

- **Trigger.** `run_triage_loop` runs in `agentline serve` (and `api.dev --worker`) and scans
  every 10 s for: executions that ended `dead` or `needs-review`, runs that ended so without
  one, and agent nodes that finished with an **empty result** (None, blank text, or a dict or
  list of empty values). One triage per failure: the key is the execution id + its manual
  retry count + the trigger, so a node that fails again after a retry gets a new triage.
  Failures from before the loop first ran in a data folder (`<home>/triage/state.json`) are
  left alone; click Triage for those.
- **Classify.** The Chat's agent (Settings → Agents → "Chat uses"; `assistant._ask`) gets the
  evidence `fixit.for_run` collects (errors, failed tasks, journal, transcript tail), redacted,
  plus the pipeline YAML, the node's prompt and output_schema files and the current Chat
  instructions. It replies with `{verdict, summary, reasoning, node, lesson, report, actions}`.
  Everything it says is redacted again before it's kept.
- **Proposals.** Its actions are checked like Chat proposals (`assistant.check_actions` with
  LIVE STATE): a pipeline edit is validated in a scratch copy, and kept only for an
  ai-instruction verdict. Triage adds the `lesson` card and, when the node failed and the agent
  didn't propose one, a `retry` card.
- **Report (infrastructure).** `report_markdown`: what happened, why triage thinks it's
  agentline, the agent's report, the redacted evidence and a fingerprint marker. A failure with
  the same fingerprint (pipeline + node + first error line with numbers and hashes masked) that
  already has a GitHub issue gets a **comment** there instead of a new issue.
- **Issues.** The run's issue (`issues.py`, created for failed runs anyway, and for an
  infrastructure verdict on a run that didn't fail) gets a `triage` field and a one-line note,
  so Problems shows the verdict. `IssueStore.set_triage`.
- **Cost.** The agent call is recorded with `add_cost(..., note="triage", touch=False)` on the
  failed execution, so it counts against the run's budget without bumping the run's
  `updated_at` (which would look like the problem happening again to `issues.sync`). The new
  `touch` flag is on both ledgers.
- **Records.** `$AGENTLINE_HOME/triage/t<12 hex>.json`; statuses `running`, `done`, `failed`
  (the agent couldn't run or gave no verdict; the loop doesn't retry it, so a missing `claude`
  CLI costs one failed record per failure: Triage again redoes it), `skipped` (budget).

## GitHub reporting (optional)

```toml
[report]
github_repo = "owner/name"   # where infrastructure reports go
github_auto = false          # true: create the issue right away instead of a card
labels = ["agentline", "triage"]
```

Plus the `GITHUB_TOKEN` key in the secrets file (ask the Chat: its key card takes the value
in a password field; a shell variable wins). `GITHUB_API_URL`
points at GitHub Enterprise or a fake. `GitHubClient.create_issue` / `comment_issue` were added
to `integrations/github.py`. Nothing goes to GitHub until it is configured.

## API, CLI, Chat

- `GET /api/triage?run=` → `{records, github: {repo, token_set, ready, auto, error}}`;
  `GET /api/triage/{id}`; `POST /api/triage {run, node?}` triages now (a click always redoes
  it); `POST /api/triage/{id}/github` sends the report (once).
- `POST /api/chat-instructions/lesson {text}`: appends `- text` under
  `## Lessons from failed runs` (a line already there isn't added twice).
  `chat_instructions.add_lesson`.
- `agentline triage list [--run R] [--json]`, `agentline triage show ID`.
- Chat actions (parity, docs/phase-25.md): `triage {run, node?}`, `lesson {text}`,
  `report_issue {triage}`. LIVE STATE lists recent triages. `triage` and `report_issue` wait
  for their own click (a model call; a public issue); `lesson` is part of Apply all.
- Chat links: with "failures" notifications on, each finished triage is sent once
  ("🔎 Triage of … : verdict", the summary, the report link, the proposals).

## UI

- `web/src/lib/triage.ts`: types and pure helpers (verdict → `<Status>` tone + word: AI
  instruction amber, agentline issue red, External grey; the Chat card; "Copy as GitHub issue"
  markdown; the report line). Tests: `triage.test.ts`, new cases in `assistantApply.test.ts`.
- `web/src/components/Triage.tsx`: `useTriage(run)` (polls while a triage runs or a failed run
  has none yet), `TriagePanel` (verdict, summary, Why, report, Open in Chat, Copy as GitHub
  issue, Issue #n, Triage again, and the proposals as the Chat's own `ActionCard`s), and
  `TriageChip`.
- Run view: a "Triage: …" chip in the toolbar (opens Run details at the node), the run's
  triage above the executions, the node's triage in its panel (and a Triage button on done
  nodes, for results that look wrong).
- Problems: the verdict in the list, the triage panel on a failed run's issue; `#/problems/<n>`
  opens an issue.
- Chat: "Open in Chat" puts the triage in the Chat as a card without a model call
  (`assistantInbox.push(reply)`); applying a `triage` card adds the result as a new card.
- "Copy as GitHub issue" falls back to showing the text to copy when the page has no clipboard
  access.

## Tests

`tests/ai/test_triage.py` (fake agent runner as the LLM, a fake GitHub HTTP server):
classification and proposals, prompt contents, redaction, the issue link, budget skip and cost,
GitHub create-once / comment-on-repeat / auto, empty-output detection, the loop ignoring older
failures, lessons, Chat action checks, chat text, the CLI. `test_chatlink.py`: triage
notifications are sent once. Checked in the built-in browser against a seeded data folder (run
view, Apply on a lesson and a pipeline card, Open in Chat, Problems, copy fallback).

## Not done / follow-ups

- Not run against a real model or a real GitHub repo (the repo has no remote yet).
- Triage runs one failure at a time; a burst of failures queues up behind the agent.
- Automatic triage has no GUI switch (the user chose automatic). `AGENTLINE_TRIAGE=off` turns
  the loop off; `tests/conftest.py` and the e2e harness set it, because `agentline serve`
  subprocesses would otherwise call the real Chat agent (a `claude` CLI on the machine).
- `[report]` has no GUI page yet: it's edited in agentline.toml or by asking the Chat.
