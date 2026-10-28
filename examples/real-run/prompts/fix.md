You are fixing a bug in the repository in the current directory.

## Plan
{{plan.summary}}

Cause: {{plan.cause}}

Steps:
{{plan.steps}}

## Rules
- Change only what the plan needs. Add a regression test.
- Run the tests with `python3 -m unittest -v` and make them pass.
- Commit with a message starting with "fix: ".
- If a tool you need is missing, write a line `MISSING_TOOL: <command>` and stop.

Finish with one line summarizing what you changed.
