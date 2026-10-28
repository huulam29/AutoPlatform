You are implementing Jira ticket {{ticket.key}}: {{ticket.summary}}

## Plan
{{plan.approach}}

Steps:
{{plan.steps}}
{{#review?}}

## Review feedback to address
The previous version was reviewed with state `{{review.state}}`. Address every comment:

{{review.comments}}
{{/review?}}

## Rules
- Work only inside the repository in the current directory.
- Keep the change focused on the ticket. Add or update tests.
- Run the tests before you finish.
- Commit your work with a message starting with "{{ticket.key}}: ".
- If a tool you need is missing (command not found), say so in a line starting with
  `MISSING_TOOL:` followed by the command name, and stop.
