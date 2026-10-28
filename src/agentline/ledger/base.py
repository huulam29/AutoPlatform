"""Storage interface. The engine's scheduling logic runs in Python inside `Ledger.tx()`, using
the small set of row operations below, so a new backend (Postgres) only implements these."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Run:
    id: str
    flow: str
    flow_hash: str
    payload: Any
    status: str
    dedupe_key: str | None = None
    budget_usd: float | None = None
    cost_usd: float = 0.0
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class Execution:
    id: str
    run_id: str
    node_id: str
    iteration: int
    status: str
    tokens_needed: int = 1
    tokens_live: int = 0
    tokens_skip: int = 0
    attempt: int = 1
    reclaims: int = 0
    lease_owner: str | None = None
    lease_expires: float | None = None
    lease_epoch: int = 0
    next_attempt_at: float = 0.0
    correlation_key: str | None = None
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    result_ref: str | None = None
    cost_usd: float = 0.0
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def key(self) -> str:
        return f"{self.node_id}#{self.iteration}"


@dataclass
class Effect:
    run_id: str
    node_id: str
    iteration: int
    name: str
    status: str  # intent | committed | redo
    idempotency_key: str | None = None
    result: Any = None
    advice: dict[str, Any] | None = None
    updated_at: float = 0.0


@dataclass
class Event:
    id: int
    correlation_key: str
    payload: Any
    received_at: float
    consumed_by: str | None = None


@dataclass
class Artifact:
    hash: str  # sha256 of the canonical JSON (kind json) or of the bytes (kind file)
    kind: str  # json | file
    media_type: str
    size: int
    summary: str
    storage: str  # inline (in blobs, keyed by hash) | file (at `path`)
    path: str | None = None
    created_at: float = 0.0


@dataclass
class ArtifactLink:
    """Lineage: an execution read or wrote the artifact `hash` under `name`."""

    run_id: str
    execution_id: str
    node_id: str
    iteration: int
    name: str
    hash: str
    direction: str  # read | wrote
    at: float = 0.0
    source_execution_id: str | None = None  # for reads: the execution that wrote it


@dataclass
class AuditEntry:
    """Who did what to which run (approve, reject, resolve, cancel, retry, deploy, ...)."""

    id: int
    at: float
    actor: str
    action: str
    run_id: str | None = None
    node_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class JournalEntry:
    """One line of a run's history: a node's status change, a task starting / finishing /
    failing, a retry. Append-only; `task` is the for_each item index (None for the node)."""

    id: int
    at: float
    run_id: str
    execution_id: str | None
    node_id: str | None
    iteration: int
    task: int | None
    kind: str  # status | task | retry | note
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunMessage:
    """One message of a run's conversation with its chat start node (agentline.runchat).
    `role`: user | agent | system. `detail`: run-action cards (`actions`), what applied them."""

    id: int
    run_id: str
    at: float
    role: str
    text: str
    via: str | None = None  # app | telegram:@user | slack:... | the node id for agent lines
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskRecord:
    """One item of a `for_each` node's execution. `result` is kept so a retry of the node only
    runs the tasks that didn't finish."""

    execution_id: str
    idx: int
    run_id: str
    node_id: str
    iteration: int
    status: str  # pending | running | done | failed
    attempts: int = 0
    item: Any = None
    result: Any = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


class LedgerTx(Protocol):
    # runs
    def insert_run(self, run: Run) -> None: ...
    def get_run(self, run_id: str) -> Run | None: ...
    def get_run_by_dedupe(self, dedupe_key: str) -> Run | None: ...
    def update_run(self, run_id: str, **fields: Any) -> None: ...
    def list_runs(self, status: str | None = None, limit: int = 100) -> list[Run]: ...
    def add_cost(
        self,
        run_id: str,
        execution_id: str,
        attempt: int,
        usd: float,
        note: str,
        *,
        touch: bool = True,
        on_execution: bool = True,
    ) -> float:
        """Record a cost; `touch=False` leaves the run's updated_at alone (a cost after the
        run ended, such as failure triage, isn't new activity); `on_execution=False` keeps it
        out of the execution's own cost (a run review isn't that step's spending), while it
        still counts for the run and its budget."""
        ...

    def list_costs(self, run_id: str) -> list[dict[str, Any]]: ...

    # executions
    def insert_execution(self, ex: Execution) -> None: ...
    def get_execution(self, exec_id: str) -> Execution | None: ...
    def update_execution(
        self,
        exec_id: str,
        *,
        expect_status: str | None = None,
        expect_epoch: int | None = None,
        **fields: Any,
    ) -> bool: ...
    def list_executions(self, run_id: str) -> list[Execution]: ...
    def open_execution(self, run_id: str, node_id: str) -> Execution | None: ...
    def max_iteration(self, run_id: str, node_id: str) -> int: ...
    def claim_next(self, owner: str, now: float, lease_ttl: float) -> Execution | None: ...
    def expired_leases(self, now: float) -> list[Execution]: ...
    def waiting_by_key(self, correlation_key: str) -> list[Execution]: ...
    def count_active(self) -> int:
        """Executions that are ready, running, or failed awaiting a retry."""
        ...

    # loops
    def loop_count(self, run_id: str, edge_id: str) -> int: ...
    def inc_loop_count(self, run_id: str, edge_id: str) -> int: ...

    # effects
    def get_effect(self, run_id: str, node_id: str, iteration: int, name: str) -> Effect | None: ...
    def put_effect(self, effect: Effect) -> None: ...
    def list_effects(self, run_id: str) -> list[Effect]: ...

    # blobs (content-addressed results)
    def put_blob(self, data: bytes) -> str: ...
    def get_blob(self, ref: str) -> bytes | None: ...

    # artifacts (metadata; content is a blob or a file) and lineage
    def put_artifact(self, artifact: Artifact) -> None:
        """Insert if absent (content-addressed, so an existing row is identical)."""
        ...

    def get_artifact(self, hash: str) -> Artifact | None: ...
    def add_artifact_link(self, link: ArtifactLink) -> None:
        """Insert if absent: one row per (execution, name, direction, hash)."""
        ...

    def list_artifact_links(self, run_id: str) -> list[ArtifactLink]: ...

    # events (gate approvals, webhooks)
    def insert_event(self, correlation_key: str, payload: Any, now: float) -> int: ...
    def unconsumed_event(self, correlation_key: str, since: float = 0.0) -> Event | None: ...
    def consume_event(self, event_id: int, consumer: str) -> None: ...

    # seen webhook delivery ids / event ids (replay rejection, webhook/poller dedupe)
    def claim_delivery(self, source: str, delivery_id: str, now: float) -> bool:
        """Record `(source, delivery_id)`; False if it was recorded before."""
        ...

    # audit log (append-only)
    def add_audit(
        self,
        actor: str,
        action: str,
        run_id: str | None = None,
        node_id: str | None = None,
        detail: dict[str, Any] | None = None,
        at: float | None = None,
    ) -> int: ...
    def list_audit(self, run_id: str | None = None, limit: int = 1000) -> list[AuditEntry]:
        """Oldest first: a run's entries, or the latest `limit` entries overall."""
        ...

    # journal (append-only history per run / node / task)
    def add_journal(
        self,
        run_id: str,
        kind: str,
        message: str,
        *,
        execution_id: str | None = None,
        node_id: str | None = None,
        iteration: int = 1,
        task: int | None = None,
        detail: dict[str, Any] | None = None,
        at: float | None = None,
    ) -> int: ...
    def list_journal(
        self, run_id: str, node_id: str | None = None, limit: int = 5000
    ) -> list[JournalEntry]:
        """Oldest first."""
        ...

    # run conversation (chat start node)
    def add_run_message(
        self,
        run_id: str,
        role: str,
        text: str,
        *,
        via: str | None = None,
        detail: dict[str, Any] | None = None,
        at: float | None = None,
    ) -> int: ...
    def list_run_messages(self, run_id: str, after: int = 0, limit: int = 1000) -> list[RunMessage]:
        """Oldest first, ids greater than `after`."""
        ...

    def update_run_message(self, message_id: int, detail: dict[str, Any]) -> None: ...

    # tasks (for_each items of an execution)
    def put_task(self, task: TaskRecord) -> None:
        """Insert or replace (execution_id, idx)."""
        ...

    def list_tasks(self, run_id: str, execution_id: str | None = None) -> list[TaskRecord]:
        """Ordered by execution, then idx."""
        ...


class Ledger(Protocol):
    def tx(self) -> AbstractContextManager[LedgerTx]:
        """One transaction: commits when the block exits normally, rolls back (and re-raises)
        on any exception. Transactions on one ledger behave as if run one at a time, except
        that a transaction whose first operation is `claim_next` may run concurrently with
        others (it only moves one ready/failed execution to running)."""
        ...

    def close(self) -> None: ...
