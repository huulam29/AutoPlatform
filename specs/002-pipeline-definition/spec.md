# Feature Specification: Pipeline Definition and Validation

**Feature Branch**: `N/A (reverse-engineered, no feature branch)`

**Created**: 2026-11-02

**Status**: Draft (as-built, awaiting review)

**Input**: Reverse-engineered from the code at commit `f45743c`. This spec describes what the pipeline layer does today: how a YAML file becomes a graph the engine runs, and what is refused on the way.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Describe a pipeline in a YAML file (Priority: P1)

A person writes a YAML file naming nodes, what each one reads and writes, and the edges between them, and the engine runs it. Each node is configuration, and each node type's behaviour is built in.

**Why this priority**: This is how every pipeline is defined. The editor, the Chat and the CLI all work on this file.

**Independent Test**: Load `examples/ticket-to-eod/pipeline.yaml` and confirm it validates and compiles.

**Acceptance Scenarios**:

1. **Given** a valid pipeline file, **When** it is loaded, **Then** it becomes a graph with one handler per node, by node type.
2. **Given** edges written as a chain such as `a -> b -> c`, **When** it is parsed, **Then** one edge is created per step.
3. **Given** a conditional or loop edge, **When** it is parsed, **Then** it must be written as a mapping, because conditions aren't allowed in a chain.
4. **Given** a node with a prompt that looks like a file path, **When** it is parsed, **Then** the text comes from that file, relative to the pipeline file.

---

### User Story 2 - Get told exactly what is wrong (Priority: P1)

A person who makes a mistake gets every problem at once, each with its location and, where possible, how to fix it.

**Why this priority**: Pipelines are written by people and by the Chat, and a bad file must fail before a run starts.

**Independent Test**: Validate a file with several mistakes and confirm each one is reported with its line and a hint.

**Acceptance Scenarios**:

1. **Given** a misspelled field, **When** the file is parsed, **Then** the issue names the field and suggests the closest known name.
2. **Given** a required input that isn't produced on every path to a node, **When** it is validated, **Then** the issue says so and suggests making it optional or reordering edges.
3. **Given** a cycle with no `max_loops`, **When** it is validated, **Then** the cycle is named.
4. **Given** a template that refers to an undeclared input, **When** it is validated, **Then** the issue names the reference.

---

### User Story 3 - Be stopped from an unsafe pipeline (Priority: P1)

A pipeline with a deploy that could run without an approval, or an agent that could edit files outside a workspace, is refused before it can run.

**Why this priority**: These checks are what the safety promises rest on.

**Independent Test**: Validate a pipeline with an ungated deploy-class action and confirm the issue. Do the same for an edit tool with no workspace.

**Acceptance Scenarios**:

1. **Given** a deploy-class action reachable without passing a gate, along edges or as a loop's `on_exhausted` target, **When** it is validated, **Then** it is refused.
2. **Given** `on_unknown: ai` on a deploy-class action, **When** it is validated, **Then** it is refused.
3. **Given** an agent with edit or shell tools and no workspace, **When** it is validated, **Then** it is refused.
4. **Given** a chat node with edit or shell tools, **When** it is validated, **Then** it is refused.

---

### User Story 4 - Choose how a run starts (Priority: P2)

A person can say which node takes a run's input and whether that input is JSON or text, and the Start run popup knows what to ask for.

**Why this priority**: Manually started runs need a clear input, but most pipelines work with the defaults.

**Independent Test**: Read the start information of a text pipeline and a JSON pipeline.

**Acceptance Scenarios**:

1. **Given** no `start:` section, **When** start information is read, **Then** the input is JSON, the node is the first start node and the template lists the payload fields the pipeline reads.
2. **Given** `input: text` and an agent start node whose prompt doesn't read `{{payload.text}}`, **When** it runs, **Then** the text is appended as an `## Input` section.

---

### User Story 5 - Keep prompts simple and checked (Priority: P2)

A prompt can use `{{artifact.field}}` and an optional block, and an agent's output can be checked against a schema, with one chance to fix it.

**Why this priority**: Output that doesn't match breaks the next node, so it is better caught at the node that made it.

**Independent Test**: Give a runner output that fails its schema once, then confirm the repair attempt and the final result.

**Acceptance Scenarios**:

1. **Given** output that fails its schema, **When** the first attempt ends, **Then** the runner is called once more with the errors appended.
2. **Given** output that still fails, **When** the repair ends, **Then** the node fails as a normal retryable failure.
3. **Given** a large input, **When** it is rendered in a template, **Then** it appears as a summary plus a file path, and a field reference reads from that file.

---

### Edge Cases

- Parser errors are collected, but invalid YAML, or a file that isn't a mapping, stops at once.
- Validation runs structural and data-flow checks in stages: edge problems stop it early, though the trigger is still checked.
- A template reference missing at render time is a permanent failure unless the name ends with `?`.
- An optional input used outside its `{{#x?}}` block is refused, because it would be missing on a loop's first pass.
- A router in `switch` mode lower-cases a boolean value, so `true` matches the label `true`.
- A `switch` value that is missing from the input is a permanent failure.
- An LLM router gets one repair, then takes the `default` edge if there is one, and otherwise fails retryably. It answers on the built-in harness.
- The output schema validator supports `type`, `properties`, `required`, `additionalProperties` (boolean), `items`, `enum`, `minItems`, `maxItems` and `minLength`. Any other keyword is ignored.
- Skill names are only checked once the skill library has been fetched. Before that, the run reports it.
- Pipelines are found in `*.yaml` and `*.yml` files anywhere in the folder, except dot folders and tool folders, and links resolving outside the folder are left out.
- `for_each` over a missing or null value runs no tasks, a mapping uses its values, and any other non-list value is a permanent failure.
- A `for_each` retry reuses the tasks already done, and the node fails if any task failed, after all have run.
- A `for_each` task's effects are named per task and per attempt, so a rerun really runs again.
- Old names are still read: `agent:` as `harness:`, on nodes and in `defaults:`. Setting both on one node is an issue.
- A pipeline names a single top-level trigger type: manual (the default), webhook, poll or schedule. Poll supports only Jira `issue_assigned`, at most every 30 s.

## Requirements *(mandatory)*

### Functional Requirements

**Format**

- **FR-001**: System MUST accept pipeline version 1 only, and reject unknown top-level fields, unknown node types, unknown edge fields, and fields not allowed on a node's type.
- **FR-002**: System MUST support the node types agent, action, router, gate, wait and chat, each with its required fields: `prompt` (agent, gate, chat), `uses` (action), `for` (wait), and `switch`, or `mode: llm` with a prompt (router).
- **FR-003**: Every reported problem MUST carry its path and message, plus line, column and hint where known.
- **FR-004**: `defaults:` MUST set the harness, model and max turns for agent nodes. Defaults are a pair: a node naming a different harness MUST NOT inherit the default model.
- **FR-005**: `harness:` and `runner:` MUST be mutually exclusive on an agent node.

**Graph rules**

- **FR-006**: System MUST reject edges to unknown nodes, duplicate edges, cycles with no `max_loops`, `max_loops` below 1, `max_loops` on an edge that doesn't close a loop, and `on_exhausted` that isn't `needs-review`, `fail` or a node id.
- **FR-007**: Only router edges MAY have `when`, and every router edge MUST have it. Router cases MUST be distinct, and a router MUST have at least one out-edge.
- **FR-008**: Every node MUST be reachable from a start node, counting loop edges and `on_exhausted` targets.
- **FR-009**: A required input MUST be produced on every path to the node, on the first iteration. An edge that can deliver a skip (from a router, or from a node that may itself be skipped) doesn't guarantee its source's outputs, while any edge that can't be skipped does. An optional input MUST have some producer.
- **FR-010**: A node inside a loop body MUST NOT join with a node outside it.
- **FR-011**: A chat node MUST be the pipeline's only start node, and a pipeline MUST have at most one.

**Safety rules**

- **FR-012**: A deploy-class action MUST NOT be reachable without passing a gate, along edges or through a loop's `on_exhausted` target, which continues from the loop edge's source. (Until 2026-11-02 the check followed edges only, so `on_exhausted: deploy` slipped past it; fixed, with `test_exhausted_loop_cannot_reach_a_deploy_around_its_gate`.)
- **FR-013**: `on_unknown: ai` MUST be rejected on deploy-class actions.
- **FR-014**: An agent with `edit` or `bash` tools MUST have a workspace, and a chat node MUST NOT have them.
- **FR-015**: An explicit image MUST be on `allow_images` when that list is set.
- **FR-016**: A node's harness and model MUST exist and fit together, and the harness's runner MUST be registered.
- **FR-017**: Named runners, workspace types, actions, waiters and MCP servers MUST exist. A harness kind that can't use MCP MUST be rejected for a node that lists servers.

**Templates and compile**

- **FR-018**: Templates MUST support only `{{a.b}}`, `{{name?}}` and `{{#name?}}...{{/name?}}`, with `payload`, `run`, `node` and `pipeline` as built-ins, and `for_each` adds `item` and `item_index`. `{{pipeline.dir}}` is only filled in for actions. In an agent prompt it passes validation, then fails permanently when rendered.
- **FR-019**: Every template reference MUST be a declared input or a built-in.
- **FR-020**: Compiling MUST produce one generic handler per node type, and validate the resulting graph.
- **FR-021**: An agent with an output schema MUST get one repair attempt, then fail as a retryable error.
- **FR-022**: A node's `timeout` MUST apply to actions through the worker, and to agents through the runner. [NEEDS CLARIFICATION: `timeout` is accepted on every node type, but gates, waits, routers and chat nodes compile with no timeout, so it does nothing there. `ARCHITECTURE.md` says it is recorded for gates and waits, but it isn't. Should the validator refuse it, or should it be enforced?]
- **FR-023**: A `for_each` node MUST run one task per item, at most `max_parallel` (default 4) at a time, recording each task.
- **FR-024**: A start node MUST take input as JSON (the default) or text, and a chat start node as chat. `start.template` applies to JSON only.

### Key Entities *(include if feature involves data)*

- **Pipeline**: A parsed spec and its compiled graph, named by its `name` field.
- **Node**: Identity, type, data contract (`in`, `out`), type settings and policy (`max_attempts`, `timeout`, `budget_usd`).
- **Edge**: A control-flow link, optionally labelled, optionally a bounded loop.
- **Issue**: One validation problem, with where it is and how to fix it.
- **Start Point**: Which node takes the input and in what form.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: `agentline validate` passes on every pipeline in `examples/`. (`ticket-to-eod` is cited in `CLAUDE.md`; this pass did not run the command.)
- **SC-002**: Every issue names its field path, and every issue from a parsed location names its line.
- **SC-003**: A pipeline with an ungated path to a deploy, by edge or by `on_exhausted`, is refused in 100% of cases.
- **SC-004**: An agent's output that fails its schema triggers exactly 1 repair call before the node fails.

## Assumptions

- `pipeline/roundtrip.py` (comment-preserving edits for the editor) is outside this spec.
- `validate_spec` checks the config it is given. Runner, action and waiter checks only apply when a registry is passed, so the CLI and editor behaviour depends on what they pass.
- Harness, model and source configuration, including what each harness supports, is covered by `agents.py` and `docs/harnesses-and-models.md`. This spec treats it as a check that passes or fails, and only `choose` and the start of `resolve` were read.
- Skills and MCP are outside this first pass, beyond the existence checks above.

## Differences From the Documentation

- `ARCHITECTURE.md` §9 says the schema validator covers a subset that excludes `maxItems` and `minLength`. The code supports both.
- §9 says gate and wait `timeout` is recorded. See FR-022.
- §9 doesn't describe `chat`, `harness`, `for_each`, `skills`, `mcp` or `start`, which came later and are recorded in the decision log.

## Code References

| Requirement | Evidence |
|---|---|
| FR-001 | `src/agentline/pipeline/spec.py:19`, `src/agentline/pipeline/spec.py:63`, `src/agentline/pipeline/spec.py:273`, `src/agentline/pipeline/spec.py:499` |
| FR-002 | `src/agentline/pipeline/spec.py:21`, `src/agentline/pipeline/spec.py:55`, `src/agentline/pipeline/spec.py:610` |
| FR-003 | `src/agentline/pipeline/spec.py:88` |
| FR-004 | `src/agentline/pipeline/spec.py:317`, `src/agentline/agents.py:273` |
| FR-005 | `src/agentline/pipeline/spec.py:582` |
| FR-006 | `src/agentline/pipeline/validate.py:59`, `src/agentline/pipeline/validate.py:109` |
| FR-007 | `src/agentline/pipeline/validate.py:119`, `src/agentline/pipeline/validate.py:154` |
| FR-008 | `src/agentline/pipeline/validate.py:166` |
| FR-009 | `src/agentline/pipeline/validate.py:239` |
| FR-010 | `src/agentline/pipeline/validate.py:285` |
| FR-011 | `src/agentline/pipeline/validate.py:193` |
| FR-012 | `src/agentline/pipeline/validate.py:507` |
| FR-013 | `src/agentline/pipeline/validate.py:478`, `src/agentline/context.py:239` |
| FR-014 | `src/agentline/pipeline/validate.py:428`, `src/agentline/pipeline/validate.py:436` |
| FR-015 | `src/agentline/pipeline/validate.py:492` |
| FR-016 | `src/agentline/pipeline/validate.py:335` |
| FR-017 | `src/agentline/pipeline/validate.py:315`, `src/agentline/pipeline/validate.py:456` |
| FR-018 | `src/agentline/pipeline/template.py:20`, `src/agentline/pipeline/compile.py:41`, `src/agentline/pipeline/compile.py:345`, `src/agentline/pipeline/validate.py:15` |
| FR-019 | `src/agentline/pipeline/validate.py:404`, `src/agentline/pipeline/validate.py:421` |
| FR-020 | `src/agentline/pipeline/compile.py:499` |
| FR-021 | `src/agentline/pipeline/compile.py:274`, `src/agentline/pipeline/compile.py:320`, `src/agentline/pipeline/schema.py:28` |
| FR-022 | `src/agentline/pipeline/spec.py:20`, `src/agentline/pipeline/compile.py:506`, `src/agentline/worker.py:197` |
| FR-023 | `src/agentline/pipeline/compile.py:104` |
| FR-024 | `src/agentline/pipeline/start.py:35`, `src/agentline/pipeline/validate.py:222` |

Edge-case evidence: `src/agentline/pipeline/compile.py:449` (switch), `src/agentline/pipeline/compile.py:392` (LLM router), `src/agentline/pipeline/files.py:13` (file discovery), `src/agentline/pipeline/spec.py:451` (poll interval), `src/agentline/pipeline/validate.py:408` (optional inputs).

Existing tests: `tests/pipeline/test_spec.py`, `tests/pipeline/test_run.py`, `tests/pipeline/test_for_each.py`, `tests/core/test_pipeline_files.py`, `tests/schedules/test_spec.py`.
