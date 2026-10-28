# Phase 10: one real run with the Claude Code runner

`scripts/real_run.sh` runs `examples/real-run/pipeline.yaml` against a tiny sample repo with a
real bug. It uses real headless Claude Code (`claude -p`) through agentline's `claude-code`
runner, in a `local` git workspace:

1. **plan**: read-only tools (`Read`, `Grep`, `Glob`), max 8 turns, budget $0.40. It must
   answer with JSON that matches `schemas/plan.json`; a mismatch gets one repair attempt.
2. **fix**: `read, edit, bash`, max 20 turns, budget $1.00, 15-minute timeout. It runs in a git
   worktree on branch `agentline/<run_id>`, fixes the bug, adds a test, runs the tests and commits.

The run budget is $1.50. Every call's cost is recorded from Claude Code's `total_cost_usd`.

## Run it

```bash
npm install -g @anthropic-ai/claude-code
```
```bash
claude
```
Log in once interactively (or set `ANTHROPIC_API_KEY`), then quit, and run:
```bash
scripts/real_run.sh
```

Everything lands in `chaos-out/real-run-<timestamp>/`: `run.txt` (submission and final status),
`runs-show.txt` / `.json` (executions, costs, lineage, transcripts by ref), `commits.txt` and
`diff.txt` (what Claude changed on the run's branch).

To check the kit without spending anything, a scripted stand-in edits, tests and commits for
real:
```bash
AGENTLINE_CLAUDE_BIN="$PWD/scripts/dry_claude.py" scripts/real_run.sh
```
`tests/e2e/test_real_run_kit.py` runs exactly that.

## What the dry run showed (2026-10-19)

```
run r01a1532d913947e6f448: done
  cost $0.06 / $1.50
executions
  plan#1           done          attempt 1  $0.01
  fix#1            done          attempt 1  $0.05
lineage
  plan#1           wrote plan: {name: "agentline/r01a…", sha: …, summary: "Treat discount_percent as a percentage", …}
  fix#1            read  plan (from plan#1)
  fix#1            wrote diff: 2 files changed, +6 -1: pricing.py, test_pricing.py
  fix#1            wrote branch: {name: "agentline/r01a…", sha: "…", …}
== commits on agentline/r01a1532d913947e6f448
fix: discount is a percentage
```

Two bugs found by building this kit, both fixed:
- A node with a workspace wrapped a dict output under `agent`, so `{{plan.summary}}` broke
  when `plan` had a workspace. A dict output is now merged flat over the branch info
  (decision 22).
- The sample repo needs a `.gitignore`. Otherwise `__pycache__` from the agent's test run is
  committed by the workspace's auto-commit of leftovers. Real target repos usually have one; a
  repo that doesn't will get build artifacts on agentline branches.

## The real run

**Not done yet**: the `claude` CLI isn't installed on the machine this was built on. After you
run `scripts/real_run.sh`, record here: the run id, final status, cost per node versus budget,
turns, whether the plan passed its schema on the first try, the diff, and anything surprising
(permission prompts, tool names, missing tools). The things most likely to differ from the
stand-in:
- the exact JSON fields of `claude -p --output-format json` in your CLI version (the runner reads
  `result`, `total_cost_usd`, `num_turns`, `session_id`, `is_error`);
- whether `--max-budget-usd` exists (off by default; enable with `with: { budget_flag: true }`);
- the plan answer wrapped in prose or code fences (the runner strips fences; prose before
  the JSON fails the schema once, then the repair prompt asks for JSON only).
