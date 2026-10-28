from __future__ import annotations

import pytest

from agentline.states import (
    ExecStatus as E,
)
from agentline.states import (
    InvalidTransition,
    RunStatus,
    check_exec_transition,
    derive_run_status,
)


@pytest.mark.parametrize(
    ("statuses", "started", "expected"),
    [
        ([E.READY], False, RunStatus.PENDING),
        ([E.DONE, E.READY], True, RunStatus.RUNNING),
        ([E.DONE, E.RUNNING], True, RunStatus.RUNNING),
        ([E.DONE, E.FAILED], True, RunStatus.FAILED),
        ([E.DONE, E.WAITING], True, RunStatus.WAITING),
        ([E.DONE, E.WAITING, E.NEEDS_REVIEW], True, RunStatus.NEEDS_REVIEW),
        ([E.DONE, E.DEAD, E.RUNNING], True, RunStatus.DEAD),
        ([E.DONE, E.SKIPPED], True, RunStatus.DONE),
        ([E.DONE, E.PENDING], True, RunStatus.NEEDS_REVIEW),  # stuck join
        ([E.CANCELLED], True, RunStatus.CANCELLED),
    ],
)
def test_derive_run_status(statuses: list[E], started: bool, expected: RunStatus) -> None:
    assert derive_run_status(statuses, started) == expected


def test_allowed_and_forbidden_transitions() -> None:
    check_exec_transition(E.READY, E.RUNNING)
    check_exec_transition(E.RUNNING, E.READY)  # reclaim
    check_exec_transition(E.WAITING, E.CANCELLED)
    with pytest.raises(InvalidTransition):
        check_exec_transition(E.DONE, E.RUNNING)
    with pytest.raises(InvalidTransition):
        check_exec_transition(E.PENDING, E.RUNNING)
    with pytest.raises(InvalidTransition):
        check_exec_transition(E.DONE, E.CANCELLED)
