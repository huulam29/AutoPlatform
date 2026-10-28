# Phase 38: run review and harness learning (design)

Status: **built** (on `phase-38`). Design agreed with the user first; the "As built" notes at
the end say where the build differs from the design. Borrowed from Google Research's
Harness² (`google-research/harness2`, arXiv 2610.14224): its *experience reflector* (edit rows
with verdicts, at most one transferable lesson per task), its per-domain *experience library*
read by later improvement rounds, and its *probes* (re-run a proposed edit on the same task and
compare executions, without a ground-truth score). What we don't borrow: K-candidate fan-out +
composer (one candidate per problem here), benchmark grading, auto-adoption.

Triage (phase 28) answers "why did this fail?". The review answers "how should this pipeline's
instructions change, judging by the whole run?", including runs that succeeded the hard way.

## The user's decisions (decision 71)

| Question | Answer |
|---|---|
| When does a run get reviewed? | **Automatically when the run shows signals** (below); a Review button on any run |
| Probes? | **Yes, every agent node**, coding nodes too (workspace rebuilt at the commit the node started from) |
| Where do lessons act? | **Reviewer memory + the Chat**: a lesson library per pipeline that later reviews read; prompts change **only through edit cards** the user applies |
| Check adopted edits later? | **Yes**: the next runs' signals before vs after decide helped / hurt / no effect |

## 1. Signals (`review.signals(app, run_id)`, no model call)

Read from the ledger and journal when a run ends (done, failed, cancelled):

| Signal | Source |
|---|---|
| review loops | router / edge visits beyond the first (`iteration > 1`), per loop edge |
| gate rejections | gate decisions `reject` + their comments |
| retries | node attempts > 1, manual retries (`retry_run`) |
| needs-review, dead | execution states |
| empty agent output | `triage._empty` |
| cost / duration spike | run cost or time > `spike_factor` (2.0) × the pipeline's median of its last 20 runs (needs ≥ 5) |
| triage verdicts | triage records of the run |

A run with any signal is reviewed (`[review] auto = "signals"`, or `"all"`, `"off"`).
`AGENTLINE_REVIEW=off` turns the loop off (set in `tests/conftest.py` and demo/e2e, like triage).

## 2. The review (`review.py`, `run_review_loop` in `serve` beside triage)

Waits until the run's triages are done, then asks the Chat's harness (`assistant._ask`, as
triage). Evidence, redacted: the pipeline YAML and each agent node's prompt / output_schema /
skills / model; per execution: inputs and outputs (artifact summaries), journal, transcript
tail, gate comments; the signals; the run's triage verdicts; **the pipeline's lesson library**;
earlier adopted edits and their verdicts.

Reply (checked, one repair round, like Chat replies):

```json
{
  "summary": "Two review loops: the writer kept missing the changelog entry",
  "rows": [
    {"node": "write_code", "component": "prompt", "observed": "reviewer rejected twice for a missing CHANGELOG line (iter 1 → 3)",
     "evidence": "review#1, review#2 comments", "verdict": "problem"}
  ],
  "proposals": [ /* Chat actions: pipeline edits, as triage cards */ ],
  "lesson": {"cue": "when a reviewer checks a repo convention the writer is never told about",
             "lesson": "state the convention in the writer's prompt (component: prompt of the writing node)",
             "evidence": "row 1", "confidence": "medium"}
}
```

Rules taken from Harness²'s reflector prompt: `verdict` is `problem | helped | hurt | no effect
| confounded`, and *prefer confounded* when several things changed or one run is all there is;
at most **one** lesson, between "be thorough" and this ticket's facts; `NO_ENTRY` and "keep as
is" are real answers; label workspace facts ("in this repo, …") apart from procedure; lessons
from the library are hypotheses to re-check, not facts. Failed nodes stay triage's job: the
review reads their verdicts and doesn't propose the same fix twice.

Cost: `add_cost(..., note="review", touch=False)` against the run's `budget_usd`; skipped while
it's used up (decision 63's rule).

## 3. Probes (`probe.py`)

A probe replays **one agent node** with a proposed edit applied, on the inputs it saw, and
compares with what it did.

- **Inputs.** New: the scheduler records the input digests in the execution's `detail.inputs`
  at claim time, so a probe of iteration 2 of a loop gets iteration 2's inputs. Runs from before
  this phase fall back to `resolve_inputs` (latest outputs), noted on the probe.
- **Workspace (coding nodes).** A throwaway `git clone --local` of the mirror at the branch
  artifact's `start_sha` into `<home>/probes/<id>/`, with **no `origin` remote**, so nothing can
  be pushed; removed afterwards. Container nodes use the same image. The run's branch is never
  touched.
- **Isolation.** No actions run (agent nodes only); MCP servers are **off** in probes unless
  marked `probe_safe = true`; `GITHUB_TOKEN`, Jira and chat-link keys aren't passed.
- **Compare.** The original output (and diff, transcript) is the baseline. The reviewer gets both
  as anonymous A/B plus the observed problem, and answers which one handles it better, and why.
  The proposal card shows "Probed: better / worse / same" with the reasoning and both outputs.
- **When.** Automatically for the review's proposals with confidence ≥ medium, at most
  `max_probes = 2` per review and within `probe_budget_usd` (default 1.00) and the run's
  budget; a **Probe** button on any edit card for the rest. Probe costs count against the run.

## 4. Lesson library (`<pipeline folder>/lessons.md`)

It lives with the pipeline (phase 32's one folder per pipeline), so history, export/import and
bundles carry it. One entry per lesson:

```markdown
### L3 · write_code · prompt
- cue: when a reviewer checks a repo convention the writer is never told about
- lesson: state the convention in the writer's prompt
- evidence: run r_8f2…: 2 review loops on a missing CHANGELOG line; probe better
- confidence: medium
- status: adopted 2026-10-21 (edit e_41…) → helped (loops 1.8 → 0.2 per run over 5 runs)
```

Statuses: `proposed` (card not applied yet), `adopted`, `helped`, `hurt`, `no effect`,
`dismissed`. The lesson is added by applying its card. Reviews of this pipeline read the
library; the Chat gets it in LIVE STATE when the pipeline is open. A lesson whose cue isn't
specific to this pipeline is also offered as a Chat-instructions lesson (phase 28's
`add_lesson`).

## 5. Checking adopted edits (`review.followup`, no model call)

Applying an edit card from a review records an adoption: lesson, node, component, the history
commit, and the baseline signal rates over the pipeline's last N runs. After the next
`followup_runs = 5` runs that executed that node, compare per-run rates (loops, retries,
rejections, needs-review, cost): a drop of ≥ 30 % and ≥ 0.5 per run is **helped**, a rise as
large is **hurt**, else **no effect**. The lesson's status is updated; *hurt* adds a card to
revert the edit (history diff, as the diff view's restore).

## 6. Surfaces

- **Run view:** "Review" chip in the toolbar + panel (summary, rows, proposals with probe
  results, lesson); Review / Review again button.
- **Pipeline → Learning tab:** the lesson library with statuses, adopted edits and their
  before/after numbers.
- **API:** `GET /api/reviews?run=|pipeline=`, `GET /api/reviews/{id}`, `POST /api/reviews {run}`,
  `POST /api/reviews/{id}/probe {proposal}`, `GET /api/lessons/{pipeline}`,
  `POST /api/lessons/{pipeline}` (add / set status).
- **CLI:** `agentline review list|show|run`, `agentline lessons <pipeline>`.
- **Chat parity** (docs/phase-25.md table): `review {run}`, `probe {review, proposal}`,
  `pipeline_lesson {pipeline, …}`, `revert_edit {adoption}`; LIVE STATE lists recent reviews.
  `review` and `probe` wait for their own click (model calls); `pipeline_lesson` is part of
  Apply all.
- **Chat links:** with "reviews" notifications on, a finished review is sent once.

## 7. Config

```toml
[review]
auto = "signals"        # "all" | "off"
spike_factor = 2.0
max_probes = 2
probe_budget_usd = 1.00
followup_runs = 5
```

## 8. Build order

1. `detail.inputs` at claim; `review.signals`; the review record store (`<home>/reviews/`).
2. Review prompt + reply checks + cards; `run_review_loop`; budget.
3. Lesson library read/write; follow-up verdicts.
4. Probes: artifact-only nodes first, then the clone-at-`start_sha` workspace path.
5. API, CLI, Chat actions, web (run view panel, Learning tab).

## 9. Tests

Fake harness as the reviewer (as `tests/ai/test_triage.py`): signals per kind and the spike
median; no review on a clean run; review waits for triage; cards checked; budget skip; lesson
file round trip + history; follow-up helped / hurt / no effect; probe inputs per iteration;
probe workspace has no remote and leaves the run branch's sha unchanged; MCP off in probes; web
helpers in vitest; checked in the built-in browser on a seeded data folder.

## 10. Open risks

- Probes of coding nodes cost a full agent run each; the caps above are the guard.
- Agent CLIs in probes still have network access (as in real runs); with no remote and no
  tokens they can't push, but a node prompt that calls external URLs will call them again.
- A single probe is one sample: shown as such ("1 run"), never as proof.

## As built

Modules: `review.py` (signals, records, the review, apply + adoption, follow-up, the loop),
`probe.py` (target, replay, comparison), `lessons.py` (the library file), `api/reviews.py`;
web `lib/review.ts`, `components/Review.tsx` (run view panel + chip, editor **Lessons**
tab). Tests: `tests/ai/test_review.py`, new cases in `test_assistant_parity.py`,
`web/src/lib/review.test.ts`, `assistantApply.test.ts`.

Where it differs from the design above:

- **Inputs need no new field.** Lineage already records what each execution read (`read`
  links, per iteration), so the scheduler wasn't changed; probes fall back to the latest
  outputs only for executions without links.
- **A review's edit is applied by the server**, `POST /api/reviews/{id}/apply {index}`
  (`review.apply_edit`), not by the Chat's generic apply: it keeps the edit's files as they
  were, so a *hurt* verdict offers an exact **undo card** (a pipeline card with the old YAML and
  files). `restore` would bring back only the YAML, and most edits change a prompt file. An undo
  puts the files back as they were before that edit, so a later edit to the same files goes too
  (the card's "Show changes" shows it). A file the edit added stays.
- **Container nodes are probed in a local clone** (no container); a tool only the image had
  shows as a probe failure.
- **Costs.** Review and probe calls are recorded on the run's last execution (a cost row needs
  one) with `add_cost(..., on_execution=False)` (phase 39): they count for the run's total and
  budget but not that step's own cost. Triage still charges the failed node.
- **"Reviews" chat notifications** are a new preference, off by default (Settings → Chat apps).
- **MCP:** `probe_safe = true` on an `[mcp.*]` table, or "Safe for probes" in Settings → MCP
  (the add / edit popup's last step; a "probe-safe" badge on the row) since phase 39.
- **Lesson cards** from a review are remembered as applied (`lesson_added` on the review record,
  set by `POST /api/lessons/...` with `review`) since phase 39.
- **Lessons tab** (phase 39): the editor has Graph | YAML | **Lessons**: the lesson library
  (Dismiss / Bring back, dismissed ones hidden behind "Show dismissed") and the adopted edits
  with their follow-up and undo cards. It replaced the first build's Learning dialog.
- **Lessons page** (phase 40): **Lessons** in the top bar (`#/lessons`) lists every pipeline
  that has lessons or adopted edits (`GET /api/lessons`), with a summary (lessons in use;
  edits checking / helped / hurt / no effect) and the same view per pipeline as the editor's
  tab (`LibraryView` in `components/Review.tsx`), plus "Open pipeline".

API: `GET/POST /api/reviews`, `GET /api/reviews/{id}`, `POST /api/reviews/{id}/probe`,
`POST /api/reviews/{id}/apply`, `GET /api/adoptions?flow=`, `GET/POST /api/lessons/{pipeline}`.
CLI: `agentline review list|show|run`, `agentline lessons <pipeline>`.

Checked in the built-in browser on a seeded throwaway data folder (fake runners only): the run
view's Review chip and panel (signals, findings, the edit and lesson cards, Probe details with the
prompt diff and both outputs), applying the edit (files written, adoption recorded, the lesson
marked adopted), the editor's Learning dialog (lesson, a checking and a hurt adoption) and its
undo card. Phase 39 was checked the same way: the Lessons tab (Dismiss), the lesson card staying
applied after a reload, the last step's cost without the review's, and the MCP switch writing
`probe_safe = true`.

Not done / follow-ups:

- **Not run against a real model.** The reviewer and comparer prompts are untested on a real
  Chat harness; a real review of a real run is the next thing to try (with `AGENTLINE_REVIEW`
  on, `agentline serve` reviews runs with signals by itself).
- Probes run one sample; Harness²'s several candidates + composer are not built.
- The follow-up compares runs by "trouble per run", whatever else changed meanwhile (another
  edit, different tickets): the verdict's detail says how many runs it rests on.
