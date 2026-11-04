# Tasks: Run Feedback, Metrics, Reports and Telemetry

Spec: [spec.md](spec.md) · Plan: [plan.md](plan.md) · Contract: [contracts/api.md](contracts/api.md)

## Phase 1: Setup
- [x] T001 `AGENTLINE_TELEMETRY=off` in `tests/conftest.py`

## Phase 2: Foundational (US1, US3 depend on these)
- [x] T002 [US1] `src/agentline/feedback.py`: `add`, `entries`, `summary`, pending file; tests `tests/test_feedback.py` (redaction with a fake secret, newest-wins, withdraw, node check, length limit, unknown run)
- [x] T003 [US3] `src/agentline/metrics.py`: `run_metrics` (work / queued / waiting, tokens, turns, models, feedback counts); tests `tests/test_metrics.py` with a fake clock and fake `runner_meta`
- [x] T004 [US5] `metrics.insights` (per pipeline, by day, per node, adoptions); tests in `tests/test_metrics.py`

## Phase 3: US2 feedback improves the pipeline
- [x] T005 [US2] `review.signals` gets `feedback`; `_trouble` counts thumbs-down; `signal_lines`; evidence section; record `feedback_upto`
- [x] T006 [US2] `review.find_runs` picks pending feedback runs (settle, finished, `on_feedback`, newer than `feedback_upto`), `run_review_loop` forces those; drain after save; `config` reads `on_feedback`
- [x] T007 [US2] tests `tests/ai/test_review_feedback.py` (fake harness: prompt contains the comment; re-review once; thumbs-up doesn't trigger; off switch)

## Phase 4: US4 report
- [x] T008 [US4] `src/agentline/runreport.py` (`build`, `markdown`); tests (fake secret in output summary and comment redacted, cap on timeline)

## Phase 5: US6 telemetry
- [x] T009 [US6] `src/agentline/telemetry.py` (`config`, `set_config`, payloads, `export_once`, `test_send`, loop, state); tests with a fake OTLP server (`tests/test_telemetry.py`: off by default, kill switch, one send per run, retry after failure, headers from secret never logged, no free text in payload)
- [x] T010 [US6] `serve` starts the telemetry loop

## Phase 6: API, CLI, chat links, Chat
- [x] T011 `src/agentline/api/feedback.py` + `server.py`; tests `tests/api/test_feedback_api.py`
- [x] T012 CLI `agentline runs metrics|report`, `agentline feedback`, `agentline insights`; tests
- [x] T013 chat link `/rate` and 👍/👎 buttons; tests with `fake_telegram`
- [x] T014 Chat actions `feedback`, `run_report`, `telemetry` (`ACTION_TYPES`, `_check_one`, GUIDE); tests

## Phase 7: Web
- [x] T015 `api.ts` client block + types
- [x] T016 `RunFeedback.tsx` (run + node), `RunMetrics.tsx`, `RunReport.tsx`; mount in `RunView.tsx`
- [x] T017 `Insights.tsx` + `lib/insights.ts` (+ vitest), route `#/insights` in `App.tsx`
- [x] T018 `TelemetrySetting.tsx` in Settings → General
- [x] T019 `lib/assistant.ts` `applyAction` / `actionTitle` for the three actions (+ vitest)

## Phase 8: Polish
- [x] T020 `docs/phase-58.md`, docs/phase-25.md parity rows, ARCHITECTURE.md §12 decisions 86-88 and the CLAUDE.md status line proposed in docs/phase-58.md for the integrator
- [x] T021 Full checks: pytest + ruff in a WSL clone; `npm test`, `tsc --noEmit`, `npm run build`; hardcode lint for web styles

Dependencies: T002 → T005-T008, T011-T019; T003 → T004, T008, T009, T016; T009 → T010, T018.
