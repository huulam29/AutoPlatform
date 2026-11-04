# Phase 58: run feedback, metrics, reports and telemetry

Approach: Spec Kit (specs/010-run-feedback-telemetry), Lane 2.

Request: "collect a lot of feedback when the pipeline is running, to help improve the pipeline:
logs, reports, telemetry". What already existed (JSON logs and redaction, the run journal, lineage,
costs, crash/failure reports, triage, run reviews, probes, lessons) is reused, not duplicated. This
phase adds what was missing: people's ratings, summed metrics, one-document run reports, trends, and
an optional OpenTelemetry export. User decisions (2026-11-03, spec Clarifications):

1. Collect all four: per-run/node metrics, human ratings, run reports, a trends page.
2. Local first; an optional OTLP export to a collector the user names, off by default, carrying no
   prompts, outputs or comments.
3. A thumbs-down, or any comment, triggers a run review with the feedback as evidence; thumbs-down
   count in the helped / hurt follow-up of adopted edits.

## What was built

| Part | Where |
|---|---|
| Ratings 👍 / 👎 / withdraw + comment, on a run or a node and iteration | `feedback.py`: journal rows of kind `feedback` (`detail = {rating, comment, by}`); comments redacted, at most 2,000 characters; newest per person and target counts. No ledger schema change (SQLite and Postgres both have `journal`) |
| Feedback → reviews | `review.py`: `signals()["feedback"]`, thumbs-down add to `trouble` (so follow-up verdicts see them), `has_signals` counts comments, a PEOPLE'S FEEDBACK section in the evidence and a GUIDE paragraph; negative feedback marks the run in `<home>/reviews/feedback-pending.json`; `find_runs` picks those once settled (30 s) and finished, and reviews them with `force` when their review saw older feedback only (`feedback_upto`). `[review] on_feedback = "off"` turns it off; `auto = "off"` and the run's budget still apply |
| Metrics per run and node | `metrics.py run_metrics`: work / queued / waiting time from the journal's status rows, attempts, retries, iterations, cost, tokens (input, output, cache read, cache write) and turns from `runner_meta.*` (unknown stays `null`), models, ratings. Claude Code's runner meta now keeps the CLI's `usage` |
| Trends | `metrics.insights`: finished runs per pipeline (runs, success rate, median and p90 time, cost per run, tokens, retries, loops taken, gate rejections, ratings), per day, and per node of one pipeline; adopted edits in the window |
| Run report | `runreport.py`: JSON (`build`) and Markdown (`markdown`), redacted as a whole; outputs as artifact summaries; timeline capped at 300 lines |
| OpenTelemetry | `telemetry.py`: `[telemetry] enabled / endpoint / service_name / headers_secret`; each finished run once as an OTLP/HTTP JSON trace (a span per run and per execution) and delta-sum metrics, stdlib `urllib`; state in `<home>/telemetry/state.json`; retries with backoff; runs queued over 24 h dropped. Only ids, names, statuses, counts, costs, tokens, models and times, every value through the redactor; header values from the named secret, never logged or returned. `AGENTLINE_TELEMETRY=off` stops it (set in `tests/conftest.py`). The loop runs in `serve` |
| API | `api/feedback.py`: `GET/POST /api/runs/{id}/feedback`, `GET /api/runs/{id}/metrics`, `GET /api/runs/{id}/report?format=md|json` (download), `GET /api/insights?days=&pipeline=`, `GET/PUT /api/telemetry`, `POST /api/telemetry/test`. Contract: `specs/010-run-feedback-telemetry/contracts/api.md` |
| CLI | `agentline feedback RUN [up|down|none] [-m comment] [--node N]`, `agentline runs metrics RUN`, `agentline runs report RUN [--format md|json] [-o file]`, `agentline insights [--days] [--pipeline]` |
| Chat links | `/rate <run> up|down [comment]`; run end notices (done / dead / needs-review) carry 👍 / 👎 buttons (`fu:` / `fd:`); recorded as `<platform>:@<user>`; a Chat proposal `feedback` can be applied from a chat app |
| Web | Run view: "How was this run?" thumbs + comment (and per node, for finished nodes), Metrics panel, Report dialog (Markdown / JSON, Copy, Download; Download hidden in the Mac app). Insights page `#/insights` in the top bar (`components/Insights.tsx`). Settings → General → Telemetry (`components/TelemetrySetting.tsx`). Client and helpers in `lib/feedback.ts` (`api.ts` only exports `call` / `raw` as `apiCall` / `apiRaw`, to keep the diff with PR #3 small) |
| Chat parity | `feedback`, `run_report`, `telemetry` in `ACTION_TYPES`, `_check_one`, the GUIDE and `title`; `applyAction` / `actionTitle` / `actionDetails` in `lib/assistant.ts`; rows in docs/phase-25.md. `telemetry` needs its own click (it can send data off the machine) |

## Tests

- `tests/ai/test_feedback.py`: ratings (redaction with a fake secret, newest wins, withdraw, node and
  iteration checks, limits), pending marks, metrics (fake `runner_meta` usage, unknown tokens,
  waiting vs work), insights, the report (redacted, capped), the review trigger end to end with a
  fake reviewer (prompt has the comment, re-review once, thumbs-up alone triggers nothing,
  `on_feedback` / `auto` off), trouble, API, CLI.
- `tests/ai/test_telemetry.py`: a fake OTLP collector: off by default and kill switch, one send per
  run, no free text or fake secret or header value in any payload, retry after a 503, API + Test.
- `tests/ai/test_chatlink.py`: 👎 button and `/rate`; `tests/ai/test_assistant_parity.py`: the three
  actions. Web: `lib/feedback.test.ts`, `lib/assistantApply.test.ts`.
- Run: pytest (not slow) + ruff in a WSL clone; `npm test`, `tsc --noEmit`, `npm run build`. The UI
  was checked by hand against `agentline.api.dev` on a throwaway ledger (Run view feedback +
  metrics + report, Insights, Settings → General → Telemetry).

## Not done / honest scope

- Telemetry is tested against a fake collector only, never a real OpenTelemetry Collector, Jaeger
  or Grafana. The OTLP JSON follows the spec's field names, but nothing real has parsed it yet.
- "Tool calls" are the runner's reported turns. No runner reports single tool calls.
- Feedback `by` is the app ("app"), the CLI user or the chat user; per-person identity is a separate
  phase (audit identity) that can fill it in.
- Run reviews triggered by feedback were tested with a fake reviewer only, like the rest of phase 38.

## For the integrator (CLAUDE.md / ARCHITECTURE.md are not edited on this branch)

ARCHITECTURE.md §12, proposed entries (renumber if 86-88 are taken by then):

| # | Decision | By | Phase |
|---|---|---|---|
| 86 | Collect people's ratings (👍 / 👎 + comment on a run or a node output), per-run/node metrics, run reports and trends; ratings are journal rows of kind `feedback`, so no ledger schema change | user | Phase 58 |
| 87 | Run data stays local; an optional OpenTelemetry (OTLP/HTTP JSON) export to a user-named collector, off by default, never with prompts, inputs, outputs, comments or error text | user | Phase 58 |
| 88 | A thumbs-down or a comment makes the run a review candidate (reviewed again once per batch of new feedback); thumbs-down count as trouble in adopted edits' follow-up. `[review] on_feedback = "off"` opts out | user | Phase 58 |

CLAUDE.md status line:

- Phase 58 (run feedback): ratings + comments on runs / nodes (`feedback.py`, journal kind
  `feedback`; Run view, `/rate` and 👍/👎 in chat apps, Chat `feedback`) trigger a run review
  (`[review] on_feedback`); metrics and Insights computed on read (`metrics.py`, `#/insights`);
  redacted run reports (`runreport.py`, Run view Report, `agentline runs report`); optional OTLP
  export (`telemetry.py`, `[telemetry]`, Settings → General, `AGENTLINE_TELEMETRY=off` in tests).
  `docs/phase-58.md`. Telemetry tested against a fake collector only.
