"""Run and node-execution states, and the only transitions allowed between them."""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"  # an execution is backing off before a retry
    NEEDS_REVIEW = "needs-review"
    DONE = "done"
    DEAD = "dead"
    CANCELLED = "cancelled"


class ExecStatus(StrEnum):
    PENDING = "pending"  # created, still collecting tokens from predecessors
    READY = "ready"
    RUNNING = "running"
    FAILED = "failed"  # attempt failed, retry after next_attempt_at
    WAITING = "waiting"  # parked on a correlation key (gate, wait)
    NEEDS_REVIEW = "needs-review"
    DONE = "done"
    SKIPPED = "skipped"
    DEAD = "dead"
    CANCELLED = "cancelled"


TERMINAL_RUN = frozenset({RunStatus.DONE, RunStatus.DEAD, RunStatus.CANCELLED})
TERMINAL_EXEC = frozenset(
    {ExecStatus.DONE, ExecStatus.SKIPPED, ExecStatus.DEAD, ExecStatus.CANCELLED}
)
CLAIMABLE_EXEC = frozenset({ExecStatus.READY, ExecStatus.FAILED})

E = ExecStatus
EXEC_TRANSITIONS: frozenset[tuple[ExecStatus, ExecStatus]] = frozenset(
    {
        (E.PENDING, E.READY),
        (E.PENDING, E.SKIPPED),
        (E.READY, E.RUNNING),
        (E.FAILED, E.RUNNING),
        (E.RUNNING, E.DONE),
        (E.RUNNING, E.FAILED),
        (E.RUNNING, E.WAITING),
        (E.RUNNING, E.NEEDS_REVIEW),
        (E.RUNNING, E.DEAD),
        (E.RUNNING, E.READY),  # lease expired (worker died): reclaimed
        (E.WAITING, E.DONE),
        (E.WAITING, E.DEAD),  # gate rejected
        (E.WAITING, E.READY),  # a resumable wait (chat node) got its event: run it again
        (E.NEEDS_REVIEW, E.READY),  # resolve: retry
        (E.NEEDS_REVIEW, E.DONE),  # resolve: done with a result
        (E.NEEDS_REVIEW, E.SKIPPED),  # resolve: skip
        (E.NEEDS_REVIEW, E.DEAD),  # resolve: fail
        (E.DEAD, E.READY),  # runs retry
        *((s, E.CANCELLED) for s in E if s not in TERMINAL_EXEC),
    }
)


class InvalidTransition(Exception):
    pass


def check_exec_transition(src: ExecStatus | str, dst: ExecStatus | str) -> None:
    if (ExecStatus(src), ExecStatus(dst)) not in EXEC_TRANSITIONS:
        raise InvalidTransition(f"execution cannot go {src} -> {dst}")


def derive_run_status(statuses: list[ExecStatus], any_started: bool) -> RunStatus:
    """Run status is a pure function of its executions' statuses."""
    s = set(statuses)
    if E.DEAD in s:
        return RunStatus.DEAD
    if E.NEEDS_REVIEW in s:
        return RunStatus.NEEDS_REVIEW
    if E.RUNNING in s:
        return RunStatus.RUNNING
    if E.READY in s:
        return RunStatus.RUNNING if any_started else RunStatus.PENDING
    if E.FAILED in s:
        return RunStatus.FAILED
    if E.WAITING in s:
        return RunStatus.WAITING
    if E.PENDING in s:
        # Only executions still collecting tokens, nothing that could send one: a stuck join.
        return RunStatus.NEEDS_REVIEW
    if s and s <= {E.CANCELLED}:
        return RunStatus.CANCELLED
    return RunStatus.DONE
