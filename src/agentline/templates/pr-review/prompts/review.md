You are reviewing pull request #{{payload.number}} in {{payload.repo}}.

## Diff
```diff
{{diff.stdout}}
```

## Your task
Review the change for correctness, security, missing tests and unclear code. Do not edit
any files. Be specific: name the file and line for each finding, and say why it matters.
Skip style nits a formatter would fix.

Answer with a single JSON object, and nothing else:
{
  "verdict": "approve | comment | request_changes",
  "summary": "one line",
  "body": "the review comment to post, in Markdown"
}
