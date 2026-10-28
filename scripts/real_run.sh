#!/usr/bin/env bash
# One real Claude Code run through agentline (Phase 10).
#
#   scripts/real_run.sh [workdir]
#
# Needs: the `claude` CLI on PATH and logged in (or ANTHROPIC_API_KEY set). Costs real money,
# capped by the pipeline budget (budget_usd: 1.50; plan 0.40, fix 1.00).
# Dry run without Claude (a scripted stand-in that edits, tests and commits for real):
#   AGENTLINE_CLAUDE_BIN="$PWD/scripts/dry_claude.py" scripts/real_run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${1:-$ROOT/chaos-out/real-run-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"

CLAUDE_BIN="${AGENTLINE_CLAUDE_BIN:-claude}"
if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  echo "claude CLI not found ($CLAUDE_BIN). Install it and log in first; see docs/real-run.md." >&2
  exit 1
fi

# 1. A tiny sample repo with a real bug and a failing test, as the "remote".
SAMPLE="$WORK/sample"
mkdir -p "$SAMPLE"
cat > "$SAMPLE/pricing.py" <<'PY'
def total(prices: list[float], discount_percent: float = 0.0) -> float:
    """Sum of prices after a percentage discount, rounded to cents."""
    subtotal = sum(prices)
    return round(subtotal - subtotal * discount_percent, 2)
PY
cat > "$SAMPLE/test_pricing.py" <<'PY'
import unittest

from pricing import total


class TotalTest(unittest.TestCase):
    def test_no_discount(self) -> None:
        self.assertEqual(total([10.0, 5.5]), 15.5)

    def test_ten_percent(self) -> None:
        self.assertEqual(total([100.0], 10), 90.0)


if __name__ == "__main__":
    unittest.main()
PY
printf '__pycache__/\n*.pyc\n' > "$SAMPLE/.gitignore"
git -C "$SAMPLE" init -q -b main
git -C "$SAMPLE" -c user.name=sample -c user.email=sample@example.com add -A
git -C "$SAMPLE" -c user.name=sample -c user.email=sample@example.com commit -q -m "pricing"
git init -q --bare -b main "$WORK/sample.git"
git -C "$SAMPLE" push -q "$WORK/sample.git" main

# 2. agentline config, pipeline, ledger: all inside $WORK.
mkdir -p "$WORK/pipelines"
cp -R "$ROOT/examples/real-run/." "$WORK/pipelines/"
cat > "$WORK/agentline.toml" <<TOML
pipelines_dir = "$WORK/pipelines"

[repos.sample]
remote = "$WORK/sample.git"
default_branch = "main"
TOML

export AGENTLINE_HOME="$WORK/home"
export AGENTLINE_DB="$WORK/agentline.db"
export AGENTLINE_CONFIG="$WORK/agentline.toml"
export AGENTLINE_CLAUDE_BIN="$CLAUDE_BIN"
cd "$ROOT"

uv run agentline validate "$WORK/pipelines/pipeline.yaml"

# 3. Run it to completion in-process.
TASK='test_ten_percent fails: total([100.0], 10) returns -900.0. Fix the discount so it is a percentage.'
uv run agentline run real-claude-code --payload "{\"task\": \"$TASK\"}" --wait --timeout 1200 \
  | tee "$WORK/run.txt"
RUN_ID="$(grep -Eo 'r[0-9a-f]{20}' "$WORK/run.txt" | head -1)"

# 4. What happened.
uv run agentline runs show "$RUN_ID" | tee "$WORK/runs-show.txt"
uv run agentline runs show "$RUN_ID" --json > "$WORK/runs-show.json"
BRANCH="agentline/$RUN_ID"
echo
echo "== commits on $BRANCH (in the mirror)"
git -C "$AGENTLINE_HOME/mirrors/sample.git" log --oneline "main..$BRANCH" | tee "$WORK/commits.txt" || true
echo "== diff"
git -C "$AGENTLINE_HOME/mirrors/sample.git" diff "main...$BRANCH" | tee "$WORK/diff.txt" || true
echo
echo "Everything is in $WORK (run.txt, runs-show.txt/json, commits.txt, diff.txt)."
