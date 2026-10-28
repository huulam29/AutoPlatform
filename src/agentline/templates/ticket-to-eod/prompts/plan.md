You are planning a code change for Jira ticket {{ticket.key}}.

## Ticket
**{{ticket.summary}}**

{{ticket.description}}

## Your task
Read the repository and design the change. Do not edit any files.

Answer with a single JSON object, and nothing else:
{
  "summary": "one line, imperative, under 70 characters",
  "approach": "a short paragraph on the design",
  "steps": ["ordered list of concrete changes"],
  "files": ["paths you expect to touch"],
  "risks": ["anything a reviewer should look at"]
}
