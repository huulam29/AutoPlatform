# Failure demo (Phase 0)

Shows, with numbers, what goes wrong with a naive at-least-once worker when it crashes.

| Piece | What it does |
|---|---|
| `fakes/server.py` | Fake GitHub (`POST /prs` never dedupes) + fake LLM (`POST /llm`). Own process, own SQLite, never killed. PR responses are delayed *after* the PR is created, so a killed client loses the response. LLM calls are counted on arrival, because a provider bills a call even if the client dies partway through. |
| `naive/queue.py` | SQLite queue with an SQS-style visibility timeout. Unacked jobs are redelivered. |
| `naive/worker.py` | `claim → LLM → POST /prs → ack`, nothing journaled. `CRASH_AT=after_llm\|after_pr` for deterministic tests. |
| `chaos.py` | Submits N tickets, SIGKILLs the worker at seeded random intervals, restarts it, reports from the fakes' records. Targets are pluggable (`--target`), so Phase 1 reruns it against the durable engine. |

## Run it

```bash
uv run python -m demo.chaos --jobs 50 --seed 1
uv run python -m demo.chaos --jobs 50 --expect-clean   # exit 1 on duplicates, stuck runs, missing PRs
```

Useful knobs: `--concurrency`, `--max-kills`, `--kill-min/--kill-max`, `--visibility-timeout`,
`--llm-latency min,max`, `--pr-latency min,max`. Reports go to `chaos-out/<run>/report.json`.

## Result (2026-10-18, defaults: 50 jobs, concurrency 4, kill every 0.3–1.5 s)

| seed | kills | done | PRs (expected 50) | tickets with dup PRs | LLM calls (expected 50) |
|---|---|---|---|---|---|
| 1 | 9 | 50 | 60 | 9 | 68 |
| 2 | 6 | 50 | 61 | 11 | 70 |
| 3 | 10 | 50 | 59 | 8 | 74 |

No run was lost (at-least-once delivers that). But ~1 in 5 tickets got 2–3 PRs and 36–48 % of
LLM spend was repeated work. A kill can land in two windows:
1. **After the PR exists, before the ack** (including "PR created, response not yet received").
   The redelivered job opens another PR. That's a duplicate external side effect.
2. **During or after the LLM call, before the ack.** The paid call is made again.

## Phase 1 result: the durable engine on the same harness

`uv run python -m demo.chaos --target durable --jobs 50 --expect-clean` runs the same two-step
job (`plan` = LLM call, `open_pr` = guarded effect with a check) on agentline, and submits every
ticket **twice** (duplicate webhooks) to exercise `dedupe_key`.

| seed | kills | runs done | PRs (expected 50) | dup PRs | LLM calls (expected 50) | extra ≤ kills? |
|---|---|---|---|---|---|---|
| 1 | 15 | 50 | 50 | 0 | 63 | 13 ≤ 15 yes |
| 2 | 5 | 50 | 50 | 0 | 54 | 4 ≤ 5 yes |
| 3 | 8 | 50 | 50 | 0 | 57 | 7 ≤ 8 yes |

Exactly one PR per ticket, no lost or stuck runs, 100 submissions deduped to 50 runs. Repeated
LLM calls stayed at or below one per kill, so even the strict "one repeat per crash" bar holds
(the report's bound is the looser kills × concurrency). Deterministic tests in
`tests/demo/test_durable_worker.py` crash at `after_llm`, `after_pr_post` (PR created, effect
not committed) and `after_pr`: one PR every time.

## Phase 2 result: the full ticket-to-eod graph under chaos

`uv run python -m demo.chaos --target pipeline --jobs 50 --expect-clean` runs
`examples/ticket-to-eod/pipeline.yaml` with a scripted fake agent and fake Jira/GitHub/deploy.
The harness plays reviewer (changes requested once, then approved) and approver while the
worker is SIGKILLed.

| seed | kills | done | PRs | dup PRs | deploys | dup deploys | LLM calls (expected 150) | reviews / approvals |
|---|---|---|---|---|---|---|---|---|
| 1 | 18 | 50 | 50 | 0 | 50 | 0 | 204 (+54) | 100 / 50 |
| 2 | 16 | 50 | 50 | 0 | 50 | 0 | 192 (+42) | 100 / 50 |
| 3 | 18 | 50 | 50 | 0 | 50 | 0 | 195 (+45) | 100 / 50 |

Every run looped once (implement#2), got approved, deployed exactly once and tested. Extra LLM
calls average about 3 per kill: each kill interrupts up to 4 in-flight agent calls (one per
worker slot), and each repeats once. That is within the bound (kills × concurrency), but above
"one per kill" because this pipeline keeps more paid calls in flight than the Phase 1 job.
