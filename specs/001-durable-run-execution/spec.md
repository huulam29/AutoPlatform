# Feature Specification: Durable Run Execution

**Feature Branch**: `N/A (reverse-engineered, no feature branch)`

**Created**: 2026-11-02

**Status**: Draft (as-built, awaiting review)

**Input**: Reverse-engineered from the code at commit `f45743c`. This spec describes what the engine does today, including behaviour that may not be intended. Where code and `ARCHITECTURE.md` differ, the code is what is specified, and the difference is listed under Differences From the Documentation.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Survive a crash without repeating finished work (Priority: P1)

A person running pipelines can lose a worker at any moment, even mid-step, and the run carries on from where it was. Finished steps are not repeated, and the interrupted step runs again.

**Why this priority**: This is the reason the product exists. Every other guarantee sits on it.

**Independent Test**: Run the chaos harness, which kills workers at random, and confirm every run ends cleanly with no duplicated side effects.

**Acceptance Scenarios**:

1. **Given** a step is running, **When** its worker stops heartbeating and the lease expires, **Then** the execution returns to ready without using up one of its attempts.
2. **Given** a worker was paused past its lease and another worker took over, **When** the first worker tries to save its result, **Then** the save is rejected.
3. **Given** a step that keeps killing its worker, **When** its lease has expired more than the reclaim limit allows, **Then** the execution goes dead with a message about a crash loop.
4. **Given** a step finished and saved its result, **When** any worker restarts, **Then** that step is not run again.

---

### User Story 2 - Never duplicate an external side effect that can be checked (Priority: P1)

A step that does something outside the engine, such as opening a pull request, does it once, even if a crash lands right around the call.

**Why this priority**: A repeated deploy or a duplicate pull request does real damage that a retry can't undo.

**Independent Test**: Crash a step between the external call and the save of its result, then confirm the retry finds the earlier result and does not repeat the call.

**Acceptance Scenarios**:

1. **Given** an effect already committed, **When** the step runs again, **Then** the stored result is returned and nothing external is called.
2. **Given** an effect left in the intent state and a check that finds the earlier result, **When** the step runs again, **Then** the effect is committed with that result and the action isn't repeated.
3. **Given** an effect left in the intent state with an idempotency key and no check, **When** the step runs again, **Then** the action is repeated, and the provider is trusted to remove the duplicate.
4. **Given** an effect left in the intent state with neither key nor check, under the default policy, **When** the step runs again, **Then** the execution stops at needs-review for a person to decide.

---

### User Story 3 - Stop for a person and carry on when they decide (Priority: P1)

When the engine can't safely decide, the run pauses without holding a worker, and a person can approve, reject, retry, skip, fail or finish the step. Each decision is audited.

**Why this priority**: Pausing safely is what makes unattended runs acceptable.

**Independent Test**: Drive a run to a gate and to a needs-review step, act on each, and confirm the run continues and an audit row exists.

**Acceptance Scenarios**:

1. **Given** a run waiting at a gate, **When** a permitted person approves it, **Then** the gate completes and its successors become ready.
2. **Given** a run waiting at a gate, **When** a person rejects it, **Then** the gate's execution goes dead and the run can be retried later.
3. **Given** a gate with named approvers, **When** someone else approves it, **Then** the approval is refused.
4. **Given** a needs-review execution, **When** a person resolves it with retry, skip, fail or done, **Then** the run moves on accordingly and the decision is audited in the same transaction.
5. **Given** an event arrives before the wait that needs it has parked, **When** the wait parks, **Then** it picks the event up and completes.

---

### User Story 4 - Branch, join and loop without getting stuck (Priority: P2)

A pipeline can route to one branch, join branches back together and loop a bounded number of times, and a join never waits for a branch that wasn't taken.

**Why this priority**: Review loops and conditional paths are the normal shape of a delivery pipeline.

**Independent Test**: Run a graph with a router, a join after it and a bounded loop, then confirm the skipped branch is marked skipped and the loop stops at its bound.

**Acceptance Scenarios**:

1. **Given** a router chooses one edge, **When** it completes, **Then** nodes on the other edges are skipped, and skips pass downstream.
2. **Given** a join with one live and one skipped incoming edge, **When** both have arrived, **Then** the join runs.
3. **Given** a loop edge that has reached its limit, **When** it would be followed again, **Then** the configured outcome applies: needs-review, fail, or continue at a named node.

---

### User Story 5 - Stay within budget (Priority: P2)

Paid work stops for a person when a run or a node goes over its budget, and all spending is on record even if a step crashes afterwards.

**Why this priority**: Agents can spend money quickly and unattended.

**Independent Test**: Give a run a small budget, record a cost over it, and confirm the execution goes to needs-review with the amounts.

**Acceptance Scenarios**:

1. **Given** a step records a cost that takes the run over budget, **When** the cost is recorded, **Then** it stays on record and the execution goes to needs-review with reason `budget_exceeded`.
2. **Given** a paid call with an estimated cost, **When** the estimate would exceed the budget, **Then** the call doesn't start.

---

### User Story 6 - See what a run did (Priority: P3)

A person can see each run's executions, costs, audit trail, journal, artifacts and which step read and wrote what.

**Why this priority**: Durable runs are only useful if a person can understand them after the fact.

**Independent Test**: Run a pipeline and read its lineage and journal.

**Acceptance Scenarios**:

1. **Given** a finished execution, **When** lineage is read, **Then** it lists what the execution read, with the source execution, and what it wrote.
2. **Given** an input larger than the handoff limit, **When** a step receives it, **Then** the step gets a reference with a summary and a file path, not the value.

---

### Edge Cases

- A run is dead as soon as any execution is dead, even while another branch is still running or ready. Those other executions keep running. See Differences From the Documentation.
- A run whose only remaining executions are `pending`, with nothing left to send them a token (a stuck join), is needs-review instead of hanging.
- A retryable failure with attempts left backs off exponentially, doubling from 1 s up to a 60 s cap, with ±20% jitter applied after the cap, so a delay can reach 72 s.
- An unexpected error while loading a step's inputs, other than a permanent error, leaves the execution running without a heartbeat. It recovers when its lease expires.
- A step's result is scrubbed before it is stored, so its successors see the redacted value.
- A dedupe key is global across all flows, and resubmitting one returns the existing run whatever its status, including dead or cancelled.
- A node's timeout counts as an ordinary retryable failure.
- A resolved `retry` keeps the attempt number. A run-level retry of a dead execution sets the attempt back to 1 and the reclaim count to 0. Both add one to the execution's manual-retry count.
- Run-level retry with no node named retries dead executions only. Naming a node also resolves its needs-review execution with retry.
- A gate approval picks the only waiting gate, or needs the node named when several are waiting.
- A delivered event wakes every execution waiting on its key, and is marked consumed by the first of them.
- Loop counts are not reset by a retry.

## Requirements *(mandatory)*

### Functional Requirements

**Run and execution lifecycle**

- **FR-001**: System MUST move an execution between statuses only along the allowed transitions, and reject any other transition.
- **FR-002**: System MUST derive run status from execution statuses in this order: dead, needs-review, running, ready (running if any execution has started, else pending), failed, waiting, pending (a stuck join, which is needs-review), all cancelled, otherwise done.
- **FR-003**: System MUST keep a cancelled run cancelled when its status is refreshed. [NEEDS CLARIFICATION: cancelling does not change an execution that is already dead, and a later retry puts it back to ready. The worker then runs it while the run still shows cancelled. Should retry refuse a cancelled run?]
- **FR-004**: System MUST create a run with one ready execution per start node, or return the existing run when the dedupe key is already known.
- **FR-005**: A run MUST keep the graph structure it started with across reloads of its pipeline, when that structure is still known. A settings-only change applies to running runs.

**Claiming, leases and fencing**

- **FR-006**: System MUST claim the claimable execution with the earliest retry time, then the earliest creation, whose retry time has passed, and bump its lease epoch.
- **FR-007**: Workers MUST heartbeat at a third of the lease time. A failed heartbeat MUST cancel the step.
- **FR-008**: Every worker write that changes an execution MUST be conditional on the status and lease epoch the worker holds, and a rejected write MUST leave the state unchanged.
- **FR-009**: System MUST return an execution with an expired lease to ready, count a reclaim instead of an attempt, and send it dead after more than the reclaim limit (default 20).
- **FR-010**: A step's result, its completion and its successor transitions MUST commit in one transaction.
- **FR-011**: System MUST retry a retryable failure until the node's maximum attempts (default 3), then mark it dead. A permanent error MUST go straight to dead.

**Effects**

- **FR-012**: System MUST record an effect's intent before calling it and its result after, keyed by run, node, iteration and name, not by attempt.
- **FR-013**: System MUST apply these rules to an effect found in the intent state: use the check when there is one, and redo the call if the check finds nothing. With only a key, redo the call. With neither, apply the `on_unknown` policy.
- **FR-014**: `on_unknown` MUST support `halt` (the default, needs-review), `retry` (redo the call) and `ai` (ask an advisor, whose cost counts against the run). `ai` MUST be refused on a deploy-class effect.
- **FR-015**: Effect records MUST only be written by the current lease holder.
- **FR-016**: System MUST let a person settle an unknown effect when resolving: redo it, or mark it done with a verified result.
- **FR-017**: An effect whose call raises an error stays in the intent state, so the next attempt treats it as unknown. [NEEDS CLARIFICATION: with no key and no check, a call that failed cleanly (the server refused it) goes to needs-review on the next attempt, instead of using the node's remaining retries. A probe confirmed this: one call, then `effect_unknown`. Is a clean failure meant to count as unknown?]

**Waiting, routing and loops**

- **FR-018**: A parked execution MUST hold no worker. A wait MUST complete with the event's payload as its result, unless it is resumable, in which case the event makes it ready to run again.
- **FR-019**: System MUST store an event before delivering it, and a wait that parks later MUST consume an unconsumed event on its key received since the execution was created.
- **FR-020**: A node MUST become ready once every non-loop incoming edge has delivered a token and at least one is live, and skipped when all are skips, passing skips on.
- **FR-021**: A router MUST follow the edge matching its label, else the `default` edge, and otherwise fail permanently.
- **FR-022**: A loop edge MUST start a new iteration of its target, and at its limit apply `on_exhausted`: needs-review, fail, or a node to continue at.

**Budgets, data and records**

- **FR-023**: System MUST record a cost as soon as it is reported, then send the execution to needs-review if the run or node budget is exceeded.
- **FR-024**: System MUST check the budget against an estimate before paid work.
- **FR-025**: System MUST give a step only its declared inputs, each from the latest done execution of its producer, and fail permanently when a required input was never produced.
- **FR-026**: System MUST store artifacts by content hash, inline up to 256 KiB and as files above that, with the file written before its record. Inputs over 64 KiB MUST reach steps as a reference.
- **FR-027**: System MUST record each execution's reads and writes as lineage, and redact a stored result, error, detail, attachment and audit row before writing it.
- **FR-028**: System MUST audit approve, reject, resolve, cancel and retry in the same transaction as the action.
- **FR-029**: System MUST journal execution creation and status changes made through its transition path, along with retries and task events. Claims and cancels are not journaled. [NEEDS CLARIFICATION: decision 48 says every execution status change goes to the journal, but moving to running and moving to cancelled don't. A probe showed `ready -> running` and the cancel missing. Should they be added, or should the decision be narrowed?]

### Key Entities *(include if feature involves data)*

- **Run**: One trip through a flow, with its payload, status, dedupe key, budget, cost and the structure hash it started with.
- **Execution**: One iteration of one node in a run, with its status, attempt, reclaim count, lease, tokens, correlation key, reason, detail, error and result reference.
- **Effect**: The record of one external side effect: its status, idempotency key, result and any advice.
- **Event**: A stored signal on a correlation key, such as an approval or a webhook, with who consumed it.
- **Artifact and Lineage Link**: Content-addressed output, and the record of which execution read or wrote it.
- **Audit Entry**: Who did what to which run.
- **Journal Entry**: One line of a run's append-only history.
- **Cost Record**: One recorded spend, against a run and an execution.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: `demo.chaos --target durable` ends clean over 50 jobs: no job lost and no duplicated side effect. (Reported in `CLAUDE.md`; this pass did not run it.)
- **SC-002**: A worker that has lost its lease gets 0 writes through.
- **SC-003**: A committed effect is called 0 further times across any number of retries.
- **SC-004**: A crash uses 0 of a step's attempts, and a crash-looping step stops after at most 21 expiries by default.
- **SC-005**: Every approve, reject, resolve, cancel and retry leaves exactly 1 audit row, or none if the action failed.

## Assumptions

- SQLite on one host is the default ledger. The Postgres ledger implements the same interface, and it is outside this spec because this pass did not read it. `CLAUDE.md` says it is untested against a real server.
- The Python flow API and YAML pipelines both produce the same graph, so everything here applies to both. YAML behaviour is in `002-pipeline-definition`.
- `for_each` task handling, and the chat node's run conversation, are only touched where they use the engine's resumable wait. The chat node is outside this first pass.
- The scheduled and live-reload loops in `serve` are outside this spec, except that a run keeps its structure.

## Differences From the Documentation

`ARCHITECTURE.md` is a Phase 0 design with as-built notes added later. These sections no longer match the code:

- §5 says a running execution counts first when deriving run status. The code checks dead and needs-review first.
- §7 says a reclaimed lease means `attempt + 1`. The code counts a reclaim instead, as the as-built note in §5 says.
- §10 says exceeding a budget makes a node fail with no retry, and the run needs-review. The code sends the execution itself to needs-review.
- §4 lists a table named `node_executions` and an `events` column set that differ from the actual `executions` and `events` tables.
- `Gate` correlation keys use the execution id, as §9's as-built note says, not `gate:<run_id>:<node_id>` as §8 says.

## Code References

| Requirement | Evidence |
|---|---|
| FR-001 | `src/agentline/states.py:39`, `src/agentline/scheduler.py:497` |
| FR-002 | `src/agentline/states.py:73`, `src/agentline/scheduler.py:459` |
| FR-003 | `src/agentline/scheduler.py:354`, `src/agentline/scheduler.py:461`, `src/agentline/app.py:290` |
| FR-004 | `src/agentline/scheduler.py:67` |
| FR-005 | `src/agentline/app.py:164`, `src/agentline/app.py:202`, `src/agentline/graph.py:102` |
| FR-006 | `src/agentline/ledger/sqlite.py:498` |
| FR-007 | `src/agentline/worker.py:41`, `src/agentline/worker.py:212` |
| FR-008 | `src/agentline/scheduler.py:35`, `src/agentline/ledger/sqlite.py:455` |
| FR-009 | `src/agentline/scheduler.py:260`, `src/agentline/worker.py:84` |
| FR-010 | `src/agentline/scheduler.py:109`, `src/agentline/ledger/sqlite.py:743` |
| FR-011 | `src/agentline/scheduler.py:153`, `src/agentline/scheduler.py:455`, `src/agentline/worker.py:156` |
| FR-012 | `src/agentline/context.py:222`, `src/agentline/ledger/sqlite.py:83` |
| FR-013 | `src/agentline/context.py:249` |
| FR-014 | `src/agentline/context.py:239`, `src/agentline/context.py:264` |
| FR-015 | `src/agentline/context.py:308` |
| FR-016 | `src/agentline/scheduler.py:339` |
| FR-017 | `src/agentline/context.py:259` |
| FR-018 | `src/agentline/scheduler.py:206`, `src/agentline/scheduler.py:282` |
| FR-019 | `src/agentline/scheduler.py:226`, `src/agentline/ledger/sqlite.py:662` |
| FR-020 | `src/agentline/scheduler.py:569`, `src/agentline/graph.py:6` |
| FR-021 | `src/agentline/scheduler.py:520` |
| FR-022 | `src/agentline/scheduler.py:126`, `src/agentline/scheduler.py:535` |
| FR-023 | `src/agentline/context.py:163`, `src/agentline/errors.py:23` |
| FR-024 | `src/agentline/context.py:205` |
| FR-025 | `src/agentline/scheduler.py:390` |
| FR-026 | `src/agentline/artifacts.py:29`, `src/agentline/artifacts.py:238`, `src/agentline/scheduler.py:414` |
| FR-027 | `src/agentline/scheduler.py:431`, `src/agentline/scheduler.py:118`, `src/agentline/app.py:482` |
| FR-028 | `src/agentline/app.py:263`, `src/agentline/app.py:342`, `src/agentline/app.py:393` |
| FR-029 | `src/agentline/scheduler.py:509`, `src/agentline/ledger/sqlite.py:506`, `src/agentline/scheduler.py:355` |

Existing tests: `tests/core/test_engine.py`, `tests/core/test_effects.py`, `tests/core/test_graph.py`, `tests/core/test_states.py`, `tests/ledger/test_contract.py`, `tests/artifacts/`.
