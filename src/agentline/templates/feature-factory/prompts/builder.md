Implement this spec in the repository, with tests, in small verified steps.

## Spec: {{spec.title}}
{{spec.summary}}

Requirements: {{spec.requirements}}
Acceptance: {{spec.acceptance}}

{{#verdict?}}
## Review round: fix every finding
The reviewers asked for changes:
{{verdict.changes}}
{{/verdict?}}

Commit on a new branch. Answer with JSON: {"name": "<branch>", "sha": "<commit>", "summary": "..."}
