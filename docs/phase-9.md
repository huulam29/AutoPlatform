# Phase 9: end-to-end acceptance

The v0.1 acceptance from the brief, run for real: a fake Jira ticket triggers the full
ticket-to-eod pipeline with fake GitHub and a scripted fake agent. A review requests changes
once, the loop runs, the review is then approved, a human approves the gate, and deploy and
tests run. Throughout, the chaos driver SIGKILLs the agentline process at random points.

```bash
uv sync --extra server
uv run python -m demo.e2e.run --tickets 10 --seed 1 --kill-min 0.2 --kill-max 0.8 \
  --max-kills 25 --expect-clean
uv run pytest tests/e2e          # a smaller version of the same run (slow-marked)
```

## What is real and what is fake

| Part | In the run |
|---|---|
| agentline | **real**: `agentline serve` (worker + poller + webhook API) as a subprocess, SQLite ledger |
| Jira | fake REST API (`demo/e2e/services.py`, reusing `tests/actions/fakes.py`); the trigger is a **real signed webhook** to `/webhooks/jira` (shared secret) |
| GitHub | fake REST API in the same process; reviews arrive as **real HMAC-signed webhooks** to `/webhooks/github`, plus the poller as fallback |
| git | **real**: a bare repo plays the GitHub remote; the `local` workspace makes a worktree from the mirror; the agent commits; `github.open_pr` pushes |
| agent | scripted (`demo/e2e/agent.py`, runner `python`): iteration 2 addresses the review in a new commit |
| approval | **real CLI**: `agentline approve <run> --node approve` as `AGENTLINE_USER=release-manager` |
| EOD | `demo/e2e/deploy.py`: `deploy` appends to a log and then sleeps (a kill window after the deploy took effect); `check` prints the sha if it was deployed; `test` logs a run |

The driver (`demo/e2e/run.py`) kills the server at random intervals and keeps it down for a
random time. Webhooks sent while it is down fail and are redelivered later with the same
delivery id, as GitHub and Jira do. Every Jira assignment is also delivered twice on purpose.

## What it checks
- exactly one run per ticket, all `done` (duplicate deliveries deduped);
- exactly one PR per ticket at GitHub, and GitHub saw exactly N PR creations;
- `implement` ran exactly twice (iterations 1 and 2), and the pushed branch head is the
  iteration-2 commit;
- exactly one deploy per ticket, of the iteration-2 commit;
- tests ran for every ticket;
- the audit trail has one `approve` by `release-manager` and one `deploy` row recording the
  sha and `approved_by: release-manager` for every run.

## Results (2026-10-19)

| seed | tickets | kills | webhook posts / redeliveries | PRs | deploys | test runs | clean |
|---|---|---|---|---|---|---|---|
| 1 | 10 | 9 | 70 / 30 | 10 | 10 | 10 | yes |
| 2 | 10 | 11 | 100 / 60 | 10 | 10 | 10 | yes |
| 3 | 10 | 9 | 55 / 15 | 10 | 10 | 10 | yes |

An earlier run without downtime (10 kills per seed, no redeliveries) was also clean on 3 seeds;
one seed ran the tests 12 times, which is expected: `env.test` is unguarded and safe to repeat.

## Limits
- Kills hit only the agentline process. The fake GitHub/Jira process and git are not killed.
- The driver is single-threaded, so a kill never lands in the middle of a webhook request; it
  lands between them. A lost-response window inside the server is covered by the Phase 1–2
  chaos runs and Phase 4 tests.
- The agent is scripted. A real Claude Code run is Phase 10.
