# Phase 52: start point + chat gatekeeper

A pipeline now says which node takes a run's input and how. One option is a **gatekeeper**
you talk to: it takes the request, asks follow-ups, hands it to the pipeline, then stays in
the run's conversation to report and act (with your confirmation).

## The spec

```yaml
start:
  node: intake            # optional; default: the graph's start node
  input: json | text      # optional; default json; a chat start node is always chat
  template: { task: "" }  # json only; default: the {{payload.…}} fields the templates read

nodes:
  intake:
    type: chat            # the gatekeeper
    harness: claude-code  # like an agent: harness + model (pickers: compatible_models)
    prompt: prompts/intake.md
    output_schema: schemas/request.json   # what it hands off (checked)
    out: request
```

- **json**: the payload is the JSON you give. The Start run popup is pre-filled with the
  template and lists the fields still empty.
- **text**: the payload is `{"text": …}`. An agent start node whose prompt doesn't read
  `{{payload.text}}` gets the text appended as `## Input`.
- **chat**: the payload is `{"text": <first message>}`; the start node is a `type: chat` node.

Validator (`pipeline/validate.py` `start`): `start.node` exists and has no incoming edge; at
most one chat node, which must be the only start node; a chat node has no `edit`/`bash` tools
(it runs on the agentline host, no workspace); `template` only with json input.
`pipeline/start.py` has `start_node`, `input_kind`, `default_template`, `start_info`.

## The engine

- **Resumable waits.** `ctx.wait(key, resume=True)` parks like any wait, but the event moves
  the execution WAITING → READY (a new allowed transition) so the step runs again; it reads
  what arrived itself. Messages that arrive while a turn runs wake it once when it parks.
- **The run thread.** `run_messages` (both ledgers): `add_run_message`, `list_run_messages`,
  `update_run_message`. Message 0 isn't stored: it's the payload's text (`runchat.thread`).
- **Intake** (`runchat.intake_step`, the chat node's handler): one harness turn per new
  message, prompt = guide + the node's instructions + the conversation. The reply is JSON
  `{say, action: ask|handoff|cancel, output?}`, checked (handoff output against
  `output_schema`) with one repair. `ask` parks on `run-chat:<run>:<node>`; `handoff`
  completes with `output`; `cancel` parks with `closing` and the loop cancels the run.
- **During the run** (`runchat.RunChatLoop`, started by `serve` and the dev server;
  `AGENTLINE_RUNCHAT=off` in tests and demo/e2e): posts system notes for nodes that die or need
  review, gates that wait and the run's end (once each, keyed in the message detail); when a
  note worth explaining or a message from you is after the gatekeeper's last line, it answers
  with the run's live state (statuses, errors, gate prompts, output summaries). Its
  `run_actions` (approve / reject / retry / cancel) become **cards**; `apply_card` runs one only
  when you confirm, audited `via` (`run-chat`, or `telegram:@user`…). Approving a gate that
  guards a deploy-class action asks twice (`guards_deploy`, shared with the chat links).
  Costs: `add_cost(on_execution=False, touch=False)` on the chat node's execution.
- The chat wait isn't a gate: Approvals, `waiting_gates` and the chat links' gate notices skip
  it. Review/probe skip chat nodes. Dry runs hand off at once with a stub of `output_schema`.

## API

- `GET /api/runs/start-info?pipeline=` → `{node, input, template, fields}`
- `POST /api/runs` takes `text` as well as `payload`
- `GET /api/runs/{id}/messages?after=` → `{messages, thinking}`; `POST /api/runs/{id}/messages`
  `{text}`; `POST /api/runs/{id}/messages/{mid}/apply` `{index, dismiss?}`

## Web

- **Start run** (`components/StartRun.tsx`, `lib/start.ts`): loads the start info; json →
  pretty template + "Still empty: …"; text → a text box; chat → "What do you need?" and Send.
- **Run view → Conversation** (`components/RunChat.tsx`) at the top of the run details when the
  pipeline has a chat node (also when that node is selected): your messages, the gatekeeper's,
  the pipeline's notes, "thinking…", cards with Confirm / Dismiss. Scrolled up, new messages
  show as "N new messages ↓" instead of moving what you read.
- **Editor**: `chat` in the `+` menu and inspector (Harness + Model, no low-level runner); a
  `start` badge on the start node; Pipeline panel → **Start point** (node, input, template).
- **Chat parity**: `start_run` takes `text`; new `run_message` (send to a run's gatekeeper).
  LIVE STATE lists each loaded pipeline's start input. Row in docs/phase-25.md.

## Chat apps

`/run <pipeline> <words>` for text / chat pipelines (json pipelines still take JSON; on an
error the template is shown). Gatekeeper and pipeline messages of chat runs are forwarded to the
linked chat (`forward_threads`, from runs created after linking); **reply** to one, or
`/say <run> <text>`, to answer. Cards become buttons (`rc:` confirm, a second tap for a deploy
gate, `rd:` dismiss). Gate-waiting notes aren't forwarded (the gate notice with its buttons is),
and generic run-ended notices are skipped for these runs (the thread reports the end).

## Template

`agentline new helpdesk`: intake (chat) → plan (agent) → approve (gate).

## Not done / not tried

- Tested with fakes only (FakeLLM / FunctionRunner, fake Telegram); not against a real model or
  chat app.
- The gatekeeper can't be routed back to (an edge into the chat node); it reports through the
  loop instead.
- MCP servers on a chat node are passed to the harness like an agent's, untested.
