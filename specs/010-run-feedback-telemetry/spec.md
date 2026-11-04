# Feature Specification: Run Feedback, Metrics, Reports and Telemetry

**Feature Branch**: `claude/goofy-bouman-1c07ec` (to be merged as `phase-58`)

**Created**: 2026-11-03

**Status**: Implemented (approved 2026-11-04)

**Input**: User description: "I need to collect a lot of feedback when the app pipeline is running
(the application) to help improve the pipeline. Add logs/reports/telemetry,... to support it."

## Context

Agentline already records a lot while a pipeline runs: JSON logs with redaction (`logs.py`,
`redact.py`), the run journal (every status change, task and retry), lineage (`artifact_links`),
costs per execution (`costs.py`, the Costs page), crash and failure reports (`incidents.py`,
`issues.py`), failure triage (`triage.py`) and run reviews with probes and lessons (`review.py`,
`probe.py`, `lessons.py`). What is missing, and what this feature adds:

1. **Metrics** are scattered: a run's duration, time waiting at gates, tokens, turns, retries and
   loops per node exist only as raw rows (executions, journal, `runner_meta` in execution detail)
   and are never summed up or shown.
2. **People can't say what they think of a run.** The only human signals are gate rejections and
   manual retries. A run that finished "done" with a poor result teaches the reviewer nothing.
3. **A run can't be taken away** as one document to read, share or attach to an issue.
4. **Nothing shows trends**: is this pipeline getting faster, cheaper, better rated since the last
   adopted edit?
5. **Teams with an observability stack can't see runs there.**

## Clarifications

### Session 2026-11-03

- Q: Which kinds of feedback should this phase collect? → A: All four: per-run/node metrics, human
  ratings, run reports, and a trends page.
- Q: May any of this leave the machine? → A: Everything stays local; an optional OpenTelemetry
  export (OTLP over HTTP) to a collector the user names, off by default, redacted, carrying no
  prompts, outputs or comments.
- Q: How should a person's rating feed pipeline improvement? → A: A thumbs-down, or any comment,
  is a review signal: the run is reviewed automatically with the feedback as evidence, and
  thumbs-down ratings count in the helped / hurt follow-up of adopted edits.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Rate a run or a node's output (Priority: P1)

A person looking at a finished (or still running) run in the Run view gives it a thumbs up or down
and optionally a comment ("the PR description ignored the ticket's acceptance criteria"). They can
rate one node's output the same way from that node's details. From Telegram, Slack or Discord they
rate with the 👍 / 👎 buttons under the "run finished" notice or with `/rate <run> up|down
[comment]`. The Chat can do it too ("rate run r12 down: the tests were skipped").

**Why this priority**: it is the one signal the engine cannot compute. Everything else in this
feature can be derived from what is already recorded.

**Independent Test**: rate a run down with a comment through the API; read the run's feedback and
journal and see the entry, with any fake secret in the comment redacted.

**Acceptance Scenarios**:

1. **Given** a run, **When** a person rates it down with a comment, **Then** the rating is recorded
   with who gave it (the app, the CLI user, or `telegram:@name`), when, and the redacted comment,
   and the Run view shows it.
2. **Given** a person who already rated a run, **When** they rate it again, **Then** their newest
   rating is the one that counts; the earlier one stays in the history.
3. **Given** a node of the run, **When** a person rates that node's output, **Then** the rating is
   tied to the node and iteration.
4. **Given** a comment containing a value of a configured secret, **When** it is recorded, **Then**
   the stored and shown text has the value redacted.
5. **Given** a run id that doesn't exist, or a rating other than up / down / none, **When** feedback
   is sent, **Then** it is refused with a clear message.
6. **Given** a linked chat app, **When** a run ends and its notice is sent, **Then** the notice has
   👍 / 👎 buttons that record a rating from that chat user.

---

### User Story 2 - Feedback improves the pipeline (Priority: P1)

A thumbs-down or a comment is evidence for the run review. The engine reviews the run (again, if it
was reviewed before the feedback came) with the people's feedback in the evidence, so the review's
edit cards and lesson address it. After an edit is adopted, thumbs-down ratings on later runs count
against it in the helped / hurt verdict.

**Why this priority**: the user asked for feedback "to help improve the pipeline"; feedback that
nothing reads does not.

**Independent Test**: with a fake Chat harness, rate a finished run down; run one review-loop pass;
the run gets a review whose prompt contains the comment, and its record says which feedback it saw.

**Acceptance Scenarios**:

1. **Given** a finished run with no signals and no review, **When** someone rates it down, **Then**
   the next review-loop pass reviews it, and the prompt contains the feedback.
2. **Given** a run that was already reviewed, **When** new thumbs-down or a comment arrives, **Then**
   the run is reviewed again once (feedback arriving within a short settle window is taken
   together), keeping applied cards and the lesson marker of the old record.
3. **Given** only thumbs-up ratings, **When** the loop runs, **Then** no review is triggered by them.
4. **Given** `[review] on_feedback = "off"` or `[review] auto = "off"` or the run's budget used up,
   **When** feedback arrives, **Then** no automatic review happens (a click still works).
5. **Given** an adopted edit, **When** later runs get thumbs-down ratings, **Then** each counts one
   unit of "trouble" in the before / after comparison.

---

### User Story 3 - See a run's metrics (Priority: P1)

In the Run view a Metrics panel shows, for the run and for each node: wall time, time queued, time
waiting (gates, waits, questions), attempts and retries, iterations (loops), cost, tokens in and out
(and cached, where the runner reports them), turns, and its rating counts. The same numbers come
from the API and `agentline runs metrics <run>`.

**Why this priority**: the numbers a reviewer and a person need to see where a run spent its time
and money; they exist but aren't summed.

**Independent Test**: run a pipeline with a fake runner that reports usage; ask for the run's
metrics; the token sums and durations match what the fake reported and the clock advanced.

**Acceptance Scenarios**:

1. **Given** a finished run, **When** its metrics are read, **Then** every node is listed with the
   sums above, and the run totals equal the sums of its nodes (cost: the run's own total, which also
   holds review / triage costs, is shown separately).
2. **Given** a gate that waited 10 minutes for approval, **When** metrics are read, **Then** that
   time is counted as waiting, not as work.
3. **Given** a runner that reports no tokens, **When** metrics are read, **Then** tokens show as
   unknown, not zero.
4. **Given** a run still going, **When** metrics are read, **Then** running executions count up to
   now and the run is marked as not finished.

---

### User Story 4 - Take a run away as a report (Priority: P2)

From the Run view a person opens "Report", sees the run as a readable document and copies or
downloads it (Markdown, or JSON for tools). It holds the run header, metrics per node, the timeline,
signals, people's feedback, triage verdicts and the review's summary and rows. The CLI writes the
same report (`agentline runs report <run> [--format md|json] [-o file]`), and the Chat can open it.

**Why this priority**: useful for sharing and for issues, but the feedback loop works without it.

**Independent Test**: build a report of a run whose output and comment contain a fake secret; the
secret is redacted in both formats; the JSON parses and holds the metrics.

**Acceptance Scenarios**:

1. **Given** a run, **When** its report is asked for, **Then** a Markdown and a JSON version are
   available, both redacted.
2. **Given** a run with thousands of journal lines, **When** the report is built, **Then** the
   timeline is capped and says how many lines were left out.
3. **Given** node outputs, **When** the report is built, **Then** it shows each output's short
   summary, not the whole output.

---

### User Story 5 - See trends across runs (Priority: P2)

An **Insights** page in the top bar shows, per pipeline, over the last 7 / 30 / 90 days: runs,
success rate, median and slowest (p90) duration, cost per run, tokens, retries, loops, gate
rejections and ratings, plus a by-day chart; choosing a pipeline shows its nodes ranked by time,
cost, failures and thumbs-down, and marks the days edits were adopted. `agentline insights` prints
the same.

**Why this priority**: shows whether changes helped; depends on Stories 1 and 3.

**Independent Test**: create runs over several fake days with known outcomes; the insights numbers
match by hand.

**Acceptance Scenarios**:

1. **Given** runs of two pipelines over 30 days, **When** Insights is opened, **Then** each
   pipeline shows its counts and rates for the chosen window, and the chart one bar per day.
2. **Given** a pipeline is chosen, **When** its nodes are listed, **Then** they are ranked and show
   their own failure rate and ratings.
3. **Given** no runs in the window, **When** the page opens, **Then** it says so.

---

### User Story 6 - Send traces and metrics to my own collector (Priority: P3)

A person with an OpenTelemetry collector turns on telemetry in Settings → General → Telemetry,
enters the collector's address and, if it needs one, the name of a secret holding its headers, and
presses Test. From then on every finished run is sent as one trace (a span per run and per
execution, with status, attempt, cost, tokens, model and duration as attributes) and a few metrics
(runs, cost, tokens, duration). The Chat can turn it on, off, or test it.

**Why this priority**: for teams with an observability stack. Local users have Stories 3 and 5.

**Independent Test**: point telemetry at a fake OTLP HTTP server; finish a run; the server receives
a trace with the run's spans; nothing in it contains prompts, outputs, comments or a fake secret.

**Acceptance Scenarios**:

1. **Given** telemetry is off (the default), **When** runs finish, **Then** nothing is sent anywhere.
2. **Given** telemetry on, **When** a run finishes, **Then** it is sent once, within about a minute.
3. **Given** the collector is down, **When** sending fails, **Then** the engine keeps working, logs a
   warning, and sends the run once the collector answers again (up to a cap of queued runs).
4. **Given** a headers secret name, **When** telemetry sends, **Then** the value comes from the
   secrets file or environment and is never shown, logged or returned by the API.
5. **Given** `AGENTLINE_TELEMETRY=off`, **When** serve runs, **Then** nothing is sent whatever the
   settings say (tests set it).

### Edge Cases

- Feedback on a run created by an older version, or whose pipeline was deleted: still recorded and
  shown; the review is skipped if the pipeline can't be found, as today.
- Feedback while the run is still going: recorded; the review waits until the run ends.
- Many ratings quickly (double clicks, two people): each is recorded; one re-review covers them.
- A comment longer than 2,000 characters: refused with a message (not cut silently).
- Telemetry endpoint that is not `http(s)://`: refused when saved.
- Clock skew / executions with no `started_at` (never claimed): durations count only what is known.
- Postgres ledger: works the same (no schema change).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST record ratings (`up`, `down`, or `none` to withdraw) with an optional
  comment on a run, or on a node and iteration of a run, naming who gave it and when.
- **FR-002**: Comments MUST be redacted before they are stored and are limited to 2,000 characters.
- **FR-003**: Ratings MUST be possible from the Run view, the CLI, the Chat (`feedback` action) and
  the linked chat apps (buttons on run notices and `/rate`).
- **FR-004**: A person's newest rating of a target MUST be the current one; all ratings stay in the
  run's history (the journal).
- **FR-005**: A thumbs-down or any comment on a finished run MUST make it a review candidate; one
  that was reviewed before the feedback MUST be reviewed again once per batch of new feedback,
  subject to `[review] auto`, the new `[review] on_feedback` (`"on"` default, `"off"`) and the run's
  budget.
- **FR-006**: Review evidence MUST include the people's feedback, and the review record MUST say up
  to which feedback it saw.
- **FR-007**: Thumbs-down ratings MUST count in the run's trouble score used by follow-up verdicts.
- **FR-008**: The system MUST compute per-run and per-node metrics from the ledger: wall time,
  queued time, waiting time, attempts, retries, iterations, cost, tokens (input, output, cache read,
  cache write) and turns where reported, and rating counts.
- **FR-009**: Metrics MUST be readable through the API, the CLI and the Run view.
- **FR-010**: The system MUST produce a redacted run report in Markdown and JSON, from the API, CLI,
  Run view (copy / download) and Chat.
- **FR-011**: The system MUST aggregate trends per pipeline (and per node of one pipeline) over a
  chosen window, by day, from the API, CLI and an Insights page.
- **FR-012**: The system MUST be able to export finished runs as OTLP/HTTP JSON traces and metrics
  to a user-configured endpoint, off by default, with an environment kill switch.
- **FR-013**: Telemetry MUST NOT carry prompts, outputs, inputs, comments, transcripts or error
  text; attribute values MUST pass the redactor; header values MUST come from a named secret.
- **FR-014**: Telemetry settings MUST be changeable from Settings and the Chat, with a Test that
  sends one test span and reports the result.
- **FR-015**: Every new GUI action (rate, report, telemetry settings and test) MUST have a Chat
  action checked against live state.
- **FR-016**: Nothing in this feature may slow down or break run execution: metrics and reports are
  computed on read, feedback writes one journal row, telemetry runs in its own loop.

### Key Entities

- **Feedback**: run, optional node + iteration, rating (up / down / none), comment, by, at. Stored
  as a journal row of kind `feedback` (no ledger schema change).
- **Run metrics**: derived; run totals plus one row per node.
- **Run report**: derived document (Markdown / JSON).
- **Insights**: derived aggregates per pipeline, per day and per node.
- **Telemetry settings**: `[telemetry]` table in `agentline.toml`: `enabled`, `endpoint`,
  `headers_secret`, `service_name`. Export progress in `<home>/telemetry/state.json`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A person can rate a run in two clicks from the Run view or one tap in a chat app.
- **SC-002**: 100% of thumbs-down / commented finished runs get a review (when reviews are on and
  budget allows) within two review-loop passes after the settle window.
- **SC-003**: Run metrics for a run of 200 executions return in under a second on SQLite.
- **SC-004**: No test fake secret appears in any feedback, report or telemetry payload.
- **SC-005**: With telemetry on, a finished run reaches the collector within 60 seconds.

## Assumptions

- "Tool calls" are counted as the runner's reported turns; runners don't report individual tool
  calls today, and parsing transcripts per harness is out of scope.
- Feedback ratings are per person ("by"); without per-person identity (a parallel session works on
  it) the actor is the app's default actor, the CLI user, or the chat user.
- The OTLP export uses the JSON encoding over HTTP with the standard library (no OpenTelemetry SDK,
  which Principle IV keeps out of the engine core).
- The trends page computes on read; caching is not needed at the current scale (hundreds of runs).
- Tested against fake collectors and fake chat platforms only (Principle VII).
