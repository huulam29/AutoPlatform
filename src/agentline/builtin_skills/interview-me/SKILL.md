---
name: interview-me
description: Before building a new pipeline, find out what it must do with a short interview, one question at a time. Use when the user asks for a new pipeline and hasn't said the goal, trigger, steps, approvals or outputs.
---

# Interview me (pipelines)

Ask before you build. One question per message, each with your best guess and quick-reply
choices when the answer is one of a few options. Stop as soon as you can predict the answers;
skip any question the user already answered.

1. **Goal**: what should the pipeline achieve, for whom? (your guess from their words)
2. **Trigger**: what starts a run: a Jira ticket, a GitHub event, a schedule, or by hand?
3. **Steps**: the steps in order, from start to the result.
4. **AI or script**: which steps need judgment or language (agent nodes) and which a script
   can do deterministically (shell.run actions)? Propose the split; most steps are scripts.
5. **Approvals**: where must a person approve (always before deploys and anything
   irreversible)?
6. **Outputs**: what comes out at the end and where does it go (a PR, a report, a chat
   message)?

Then summarize the answers in a few lines, and propose the pipeline.
