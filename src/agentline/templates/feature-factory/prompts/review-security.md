Security review of PR {{pr.url}} (branch {{branch.name}}) for "{{spec.title}}".
Check out the branch; look for injection, authz/authn gaps, secrets, unsafe input handling, dependency risks.
Answer with JSON: {"verdict": "PASS" | "CHANGES", "summary": "...", "findings": ["file:line: problem and fix"]}
