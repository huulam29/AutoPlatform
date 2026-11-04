# Implementation Plan: Run Feedback, Metrics, Reports and Telemetry

**Branch**: `claude/goofy-bouman-1c07ec` (merged as `phase-58`) | **Date**: 2026-11-03 |
**Spec**: [spec.md](spec.md)

## Summary

Four engine modules computed from what the ledger already holds, plus one exporter:

- `feedback.py`: ratings and comments as journal rows of kind `feedback` (redacted), a summary per
  run (current rating per person and target), and a small "pending review" file the review loop
  drains.
- `metrics.py`: per-run and per-node metrics from executions, journal status rows and
  `runner_meta.*` in execution detail; `insights()` aggregates finished runs per pipeline, day and
  node.
- `runreport.py`: a redacted run report (JSON dict + Markdown renderer).
- `telemetry.py`: `[telemetry]` config, OTLP/HTTP JSON trace + metric payloads per finished run
  (`urllib`), a loop in `serve`, a state file, a Test.
- `review.py` changes: a `feedback` signal (counts in `trouble`), a PEOPLE'S FEEDBACK evidence
  section, `find_runs` picks runs with new negative feedback, `[review] on_feedback`.

API (new router `api/feedback.py`), CLI (`runs metrics`, `runs report`, `feedback`, `insights`),
chat-link `/rate` and 👍/👎 buttons, Chat actions `feedback`, `run_report`, `telemetry`, and web:
Run view Feedback + Metrics panels and Report dialog, Insights page, Settings → General → Telemetry.

## Technical Context

**Language/Version**: Python 3.11+ (engine), TypeScript + React (web)
**Primary Dependencies**: stdlib only in the engine (`urllib`, `json`, `statistics`); FastAPI for
the router (server extra). No OpenTelemetry SDK.
**Storage**: ledger journal rows (no schema change; SQLite and Postgres both have `journal`),
`<home>/telemetry/state.json`, `<home>/reviews/feedback-pending.json`, `[telemetry]` in
`agentline.toml` (via `tomlwrite.set_plain_table`).
**Testing**: pytest (in a WSL clone), fake OTLP HTTP server (`_Server` trick), fake Chat harness,
fake Telegram; vitest, tsc, vite build.
**Constraints**: computing on read must stay cheap (SC-003): one tx per run, no artifact loads for
metrics; insights capped at 2,000 runs per window.

## Constitution Check

| Principle | How this plan complies |
|---|---|
| I Durable | Feedback is a plain journal insert in its own tx; no state transitions, no effects. Telemetry runs outside the scheduler; a failed send never touches runs. |
| II People decide | Reviews triggered by feedback still only propose cards; nothing applies automatically. Feedback is not an approval and writes no audit row (it is the person's opinion, recorded with `by`). |
| III Secrets | Comments go through `app.redactor.redact_text` before the insert; reports run `redact_value` over the whole document; telemetry carries no free text, its attributes pass the redactor, header values are read from a named secret and never logged or returned. Tests try to leak fake secrets through each path. |
| IV One engine | No per-node code; stdlib OTLP JSON; no new runtime deps. Abstractions: one module per concern, each with a named need from the spec. |
| V Least privilege | Unchanged. |
| VI Tested | Tests per module, API, CLI, chat link, Chat parity, web. `AGENTLINE_TELEMETRY=off` in `tests/conftest.py`; review loop stays off in tests (`AGENTLINE_REVIEW=off`), its feedback pass is tested by calling `find_runs` / `review` with a fake harness. |
| VII Honest scope | docs/phase-58.md says telemetry was tested against a fake collector only, and that "tool calls" are runner turns. |
| Workflow | Chat actions for every new GUI action; ARCHITECTURE decision log entries 86-88 for the three user decisions. |

No exceptions needed.

## Design decisions

1. **Feedback lives in the journal** (`kind = "feedback"`, `detail = {rating, comment, by,
   target}`), not a new table: durable, per run / node / iteration, already in both ledgers and in
   the Run view's journal, append-only (the newest rating per `(by, node, iteration)` counts). A new
   `LedgerTx` method is not needed: `add_journal` / `list_journal` exist. No `runs.updated_at` bump
   (`add_journal` doesn't touch it), so issues.sync isn't confused.
2. **Re-review trigger**: `feedback.add` appends `{run, id, at}` to
   `<home>/reviews/feedback-pending.json` when the entry is negative (down or comment). `find_runs`
   reads it: a pending run that is finished, settled for `FEEDBACK_SETTLE` (30 s) since its newest
   pending entry, and whose review's `feedback_upto` is below the newest id, is reviewed with
   `force=True`. Drained after the review is saved (also when skipped for budget). The CLI and API
   write the same file, so feedback given while `serve` is down is picked up later.
3. **Metrics from the journal**: `waiting` time = sum of intervals from `* -> waiting` to the next
   status row of that execution (or now); queued = `started_at - created_at` of the first claim;
   work = `finished_at - started_at` minus waiting. Tokens from every `runner_meta.*` entry's
   `usage` (keys `input_tokens`/`prompt_tokens`, `output_tokens`/`completion_tokens`,
   `cache_read_input_tokens`/`cached_input_tokens`, `cache_creation_input_tokens`), `None` when no
   runner reported usage. Turns from `turns`.
4. **Insights from runs + executions only** (no artifact reads): loops = extra iterations, retries =
   attempts - 1, rejections = dead gate executions, ratings from feedback journal rows.
5. **OTLP JSON over HTTP** to `{endpoint}/v1/traces` and `/v1/metrics`; trace id = sha256 of the
   run id (stable, so a resend is the same trace); span ids from execution ids. Metrics as delta
   sums per run (`agentline.runs`, `agentline.cost_usd`, `agentline.tokens`, `agentline.run.duration_s`).
   A state file keeps a watermark (`since`) and recently sent ids; batches of 20 runs; on failure,
   retry with backoff, and runs older than 24 h in the queue are dropped with a warning.
6. **Report**: built from `metrics.run_metrics`, `feedback.summary`, `review.signals` lines, the
   triage and review records, and the journal (capped at 300 lines); outputs only as artifact
   summaries. Markdown renderer is a pure function of the JSON.

## Project Structure

```text
src/agentline/feedback.py          new
src/agentline/metrics.py           new
src/agentline/runreport.py         new
src/agentline/telemetry.py         new
src/agentline/review.py            feedback signal, evidence, find_runs, on_feedback
src/agentline/api/feedback.py      new router: feedback, metrics, report, insights, telemetry
src/agentline/api/server.py        include the router
src/agentline/cli/__init__.py, commands.py   runs metrics / runs report / feedback / insights
src/agentline/cli/__init__.py (serve)         telemetry loop
src/agentline/chatlink/service.py  /rate, 👍/👎 on run notices
src/agentline/assistant.py         ACTION_TYPES feedback / run_report / telemetry, checks, GUIDE
tests/test_feedback.py, test_metrics.py, test_runreport.py, test_telemetry.py,
tests/api/test_feedback_api.py, tests/ai/test_review_feedback.py, chat link + parity tests
web/src/api.ts                     client calls (small, appended block)
web/src/components/RunFeedback.tsx, RunMetrics.tsx, RunReport.tsx, Insights.tsx,
web/src/components/TelemetrySetting.tsx       new
web/src/components/RunView.tsx, App.tsx, SettingsExtra.tsx (one mount line each)
web/src/lib/insights.ts (+ test), lib/assistant.ts (applyAction / actionTitle)
docs/phase-58.md, docs/phase-25.md (Chat parity rows), ARCHITECTURE.md §12 (86-88)
```

**Conflicts with PR #3** (`App.tsx`, `SettingsExtra.tsx`, `api.ts`): additions only, in new
components, with one-line mounts and an appended block in `api.ts`.

## Complexity Tracking

None.
