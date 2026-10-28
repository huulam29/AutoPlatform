"""Ledger contract: every `LedgerTx` operation and the semantics the engine relies on, run
against every backend (tests/ledger/conftest.py). Nothing here may depend on a backend's
internals: only `Ledger.tx()` and the `LedgerTx` protocol."""

from __future__ import annotations

import asyncio
import inspect
import threading
from pathlib import Path
from typing import Any

import pytest

from agentline import App, RunStatus, StepContext
from agentline.ids import content_hash
from agentline.ledger.base import (
    Artifact,
    ArtifactLink,
    Effect,
    Execution,
    Ledger,
    LedgerTx,
    Run,
)

from .conftest import Backend

T0 = 1_700_000_000.0


def mk_run(run_id: str = "r1", dedupe_key: str | None = None, **kw: Any) -> Run:
    base: dict[str, Any] = dict(
        id=run_id,
        flow="f",
        flow_hash="h",
        payload={"ticket": run_id},
        status="running",
        dedupe_key=dedupe_key,
        created_at=T0,
        updated_at=T0,
    )
    base.update(kw)
    return Run(**base)


def mk_exec(exec_id: str, run_id: str = "r1", node_id: str = "a", **kw: Any) -> Execution:
    base: dict[str, Any] = dict(
        id=exec_id, run_id=run_id, node_id=node_id, iteration=1, status="ready", created_at=T0
    )
    base.update(kw)
    return Execution(**base)


def seed(ledger: Ledger, *execs: Execution, runs: tuple[str, ...] = ("r1",)) -> None:
    with ledger.tx() as tx:
        for r in runs:
            tx.insert_run(mk_run(r))
        for ex in execs:
            tx.insert_execution(ex)


# -- coverage ---------------------------------------------------------------------------------
def test_suite_exercises_every_ledgertx_operation() -> None:
    ops = [n for n, _ in inspect.getmembers(LedgerTx, inspect.isfunction) if not n.startswith("_")]
    source = Path(__file__).read_text()
    missing = [n for n in ops if f"tx.{n}(" not in source]
    assert not missing, f"contract suite never calls: {missing}"


# -- runs -------------------------------------------------------------------------------------
def test_run_round_trip_and_update(ledger: Ledger) -> None:
    run = mk_run(payload={"a": [1, 2.5, None, True], "ü": "naïve ✓", "n": {"x": {}}})
    with ledger.tx() as tx:
        tx.insert_run(run)
    with ledger.tx() as tx:
        assert tx.get_run("r1") == run
        assert tx.get_run("nope") is None
        tx.update_run("r1", status="done", error="e", payload=[1, "two"], updated_at=T0 + 5)
    with ledger.tx() as tx:
        got = tx.get_run("r1")
    assert got is not None
    assert (got.status, got.error, got.payload, got.updated_at) == ("done", "e", [1, "two"], T0 + 5)
    with ledger.tx() as tx:
        tx.update_run("r1", status="failed")  # updated_at defaults to now
        got = tx.get_run("r1")
    assert got is not None and got.updated_at > T0 + 5


@pytest.mark.parametrize("payload", [None, 0, "s", [], {}, [{"k": 1.25}], {"big": 2**53}])
def test_any_json_payload_round_trips(ledger: Ledger, payload: Any) -> None:
    with ledger.tx() as tx:
        tx.insert_run(mk_run(payload=payload))
    with ledger.tx() as tx:
        got = tx.get_run("r1")
    assert got is not None and got.payload == payload


def test_insert_run_dedupe_key_is_unique(ledger: Ledger) -> None:
    with ledger.tx() as tx:
        tx.insert_run(mk_run("r1", dedupe_key="jira:T-1"))
        tx.insert_run(mk_run("r2"))  # NULL dedupe keys never collide
        tx.insert_run(mk_run("r3"))
    with pytest.raises(Exception), ledger.tx() as tx:  # noqa: B017 - driver-specific type
        tx.insert_run(mk_run("r4", dedupe_key="jira:T-1"))
    with ledger.tx() as tx:  # the failed transaction left nothing behind; the ledger works
        assert tx.get_run("r4") is None
        found = tx.get_run_by_dedupe("jira:T-1")
        assert found is not None and found.id == "r1"
        assert tx.get_run_by_dedupe("jira:T-2") is None


def test_duplicate_run_id_rejected(ledger: Ledger) -> None:
    with ledger.tx() as tx:
        tx.insert_run(mk_run("r1"))
    with pytest.raises(Exception), ledger.tx() as tx:  # noqa: B017
        tx.insert_run(mk_run("r1"))


def test_list_runs_newest_first_with_status_and_limit(ledger: Ledger) -> None:
    with ledger.tx() as tx:
        for i in range(5):
            status = "done" if i % 2 else "running"
            tx.insert_run(mk_run(f"r{i}", created_at=T0 + i, status=status))
    with ledger.tx() as tx:
        assert [r.id for r in tx.list_runs()] == ["r4", "r3", "r2", "r1", "r0"]
        assert [r.id for r in tx.list_runs(limit=2)] == ["r4", "r3"]
        assert [r.id for r in tx.list_runs("done")] == ["r3", "r1"]
        assert [r.id for r in tx.list_runs("running", limit=1)] == ["r4"]
        assert tx.list_runs("cancelled") == []


def test_run_level_cost_leaves_the_execution_alone(ledger: Ledger) -> None:
    """A run review's cost: the run's total and budget, not the step it is recorded on."""
    seed(ledger, mk_exec("e1"))
    with ledger.tx() as tx:
        tx.add_cost("r1", "e1", 1, 0.25, "plan")
        assert tx.add_cost("r1", "e1", 1, 0.1, "review", on_execution=False) == pytest.approx(0.35)
    with ledger.tx() as tx:
        e1 = tx.get_execution("e1")
        assert e1 is not None and e1.cost_usd == pytest.approx(0.25)
        assert [c["note"] for c in tx.list_costs("r1")] == ["plan", "review"]


def test_costs_accumulate_on_run_and_execution(ledger: Ledger) -> None:
    seed(ledger, mk_exec("e1"), mk_exec("e2", node_id="b"))
    with ledger.tx() as tx:
        assert tx.add_cost("r1", "e1", 1, 0.25, "plan") == pytest.approx(0.25)
        assert tx.add_cost("r1", "e2", 2, 0.5, "impl") == pytest.approx(0.75)
    with ledger.tx() as tx:
        run = tx.get_run("r1")
        e1, e2 = tx.get_execution("e1"), tx.get_execution("e2")
        costs = tx.list_costs("r1")
        assert tx.list_costs("other") == []
    assert run is not None and run.cost_usd == pytest.approx(0.75)
    assert e1 is not None and e1.cost_usd == pytest.approx(0.25)
    assert e2 is not None and e2.cost_usd == pytest.approx(0.5)
    assert [(c["execution_id"], c["attempt"], c["usd"], c["note"]) for c in costs] == [
        ("e1", 1, 0.25, "plan"),
        ("e2", 2, 0.5, "impl"),
    ]
    assert {"id", "run_id", "at"} <= set(costs[0])


# -- executions -------------------------------------------------------------------------------
def test_execution_round_trip(ledger: Ledger) -> None:
    ex = mk_exec(
        "e1",
        tokens_needed=2,
        tokens_live=1,
        tokens_skip=1,
        attempt=3,
        reclaims=1,
        lease_owner="w1",
        lease_expires=T0 + 9.5,
        lease_epoch=4,
        next_attempt_at=T0 + 1,
        correlation_key="github:pr:1",
        reason="r",
        detail={"kind": "gate", "nested": {"l": [1, "x"]}},
        error="boom",
        result_ref="abc",
        cost_usd=0.125,
        started_at=T0 + 2,
        finished_at=T0 + 3,
    )
    seed(ledger, ex)
    with ledger.tx() as tx:
        assert tx.get_execution("e1") == ex
        assert tx.get_execution("nope") is None


def test_execution_key_unique_per_run_node_iteration(ledger: Ledger) -> None:
    seed(ledger, mk_exec("e1"))
    with pytest.raises(Exception), ledger.tx() as tx:  # noqa: B017
        tx.insert_execution(mk_exec("e2"))  # same (r1, a, 1)
    with ledger.tx() as tx:
        tx.insert_execution(mk_exec("e2", iteration=2))
        assert [x.id for x in tx.list_executions("r1")] == ["e1", "e2"]


def test_fenced_update_execution(ledger: Ledger) -> None:
    seed(ledger, mk_exec("e1", status="running", lease_epoch=3))
    with ledger.tx() as tx:
        assert not tx.update_execution("e1", expect_status="ready", status="done")
        assert not tx.update_execution("e1", expect_epoch=2, status="done")
        assert not tx.update_execution("e1", expect_status="running", expect_epoch=4, status="done")
        assert not tx.update_execution("missing", status="done")
        ex = tx.get_execution("e1")
        assert ex is not None and ex.status == "running"
        assert tx.update_execution(
            "e1",
            expect_status="running",
            expect_epoch=3,
            status="done",
            detail={"note": 1},
            lease_owner=None,
        )
        assert tx.update_execution("e1", reason="unfenced")
    with ledger.tx() as tx:
        ex = tx.get_execution("e1")
    assert ex is not None
    assert (ex.status, ex.detail, ex.lease_owner, ex.reason) == (
        "done",
        {"note": 1},
        None,
        "unfenced",
    )


def test_list_executions_open_execution_max_iteration(ledger: Ledger) -> None:
    seed(
        ledger,
        mk_exec("e1", node_id="a", iteration=1, status="done", created_at=T0),
        mk_exec("e2", node_id="j", iteration=1, status="done", created_at=T0 + 1),
        mk_exec("e3", node_id="j", iteration=2, status="pending", created_at=T0 + 2),
        mk_exec("e4", node_id="j", iteration=3, status="pending", created_at=T0 + 3),
        mk_exec("e5", node_id="j", iteration=4, status="ready", created_at=T0 + 4),
        mk_exec("x1", run_id="r2", node_id="j", iteration=7, status="pending"),
        runs=("r1", "r2"),
    )
    with ledger.tx() as tx:
        assert [x.id for x in tx.list_executions("r1")] == ["e1", "e2", "e3", "e4", "e5"]
        assert tx.list_executions("nope") == []
        opened = tx.open_execution("r1", "j")
        assert opened is not None and opened.id == "e4"  # highest pending iteration
        assert tx.open_execution("r1", "a") is None
        assert tx.open_execution("r1", "zzz") is None
        assert tx.max_iteration("r1", "j") == 4
        assert tx.max_iteration("r1", "a") == 1
        assert tx.max_iteration("r1", "zzz") == 0
        assert tx.max_iteration("r2", "j") == 7


def test_claim_next_order_and_lease(ledger: Ledger) -> None:
    seed(
        ledger,
        mk_exec("late", node_id="a", next_attempt_at=T0 + 100),
        mk_exec("retry", node_id="b", status="failed", next_attempt_at=T0 + 2, attempt=2),
        mk_exec("first", node_id="c", next_attempt_at=T0 + 1, lease_epoch=5),
        mk_exec("pending", node_id="d", status="pending"),
        mk_exec("waiting", node_id="e", status="waiting"),
        mk_exec("running", node_id="f", status="running"),
    )
    with ledger.tx() as tx:
        ex = tx.claim_next("w1", T0 + 10, 30.0)
    assert ex is not None and ex.id == "first"
    assert (ex.status, ex.lease_owner, ex.lease_epoch) == ("running", "w1", 6)
    assert ex.lease_expires == pytest.approx(T0 + 40) and ex.started_at == pytest.approx(T0 + 10)
    with ledger.tx() as tx:
        ex = tx.claim_next("w2", T0 + 10, 30.0)
        assert ex is not None and (ex.id, ex.status, ex.attempt) == ("retry", "running", 2)
        assert tx.claim_next("w2", T0 + 10, 30.0) is None  # "late" is not due yet
        late = tx.claim_next("w3", T0 + 100, 1.0)
        assert late is not None and late.id == "late"
        assert tx.claim_next("w3", T0 + 1000, 1.0) is None
    with ledger.tx() as tx:
        stored = tx.get_execution("first")
    assert stored is not None and (stored.status, stored.lease_owner) == ("running", "w1")


def _claim_all(ledger: Ledger, owner: str, out: list[str], errors: list[BaseException]) -> None:
    try:
        while True:
            with ledger.tx() as tx:
                ex = tx.claim_next(owner, T0 + 10, 60.0)
            if ex is None:
                return
            out.append(ex.id)
    except BaseException as e:  # surfaced by the test
        errors.append(e)


@pytest.mark.parametrize("shared", [False, True], ids=["own-ledgers", "shared-ledger"])
def test_concurrent_claims_never_double_claim(backend: Backend, shared: bool) -> None:
    n, workers = 80, 8
    first = backend.open()
    seed(first, *(mk_exec(f"e{i:03d}", node_id=f"n{i}") for i in range(n)))
    ledgers = [first if shared else backend.open() for _ in range(workers)]
    claimed: list[list[str]] = [[] for _ in range(workers)]
    errors: list[BaseException] = []
    start = threading.Barrier(workers)

    def go(i: int) -> None:
        start.wait()
        _claim_all(ledgers[i], f"w{i}", claimed[i], errors)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    all_ids = [x for c in claimed for x in c]
    assert len(all_ids) == len(set(all_ids)) == n
    with first.tx() as tx:
        execs = tx.list_executions("r1")
        assert tx.count_active() == n  # all running now
    by_id = {x.id: x for x in execs}
    for i, ids in enumerate(claimed):
        for x in ids:
            assert by_id[x].lease_owner == f"w{i}" and by_id[x].lease_epoch == 1


def test_expired_leases_waiting_by_key_count_active(ledger: Ledger) -> None:
    seed(
        ledger,
        mk_exec("run_old", node_id="a", status="running", lease_expires=T0 + 1),
        mk_exec("run_new", node_id="b", status="running", lease_expires=T0 + 100),
        mk_exec("ready", node_id="c"),
        mk_exec("failed", node_id="d", status="failed"),
        mk_exec("w1", node_id="e", status="waiting", correlation_key="pr:1"),
        mk_exec("w2", node_id="f", status="waiting", correlation_key="pr:2"),
        mk_exec("done1", node_id="g", status="done", correlation_key="pr:1"),
        mk_exec("pend", node_id="h", status="pending"),
    )
    with ledger.tx() as tx:
        assert [x.id for x in tx.expired_leases(T0 + 50)] == ["run_old"]
        assert sorted(x.id for x in tx.expired_leases(T0 + 1000)) == ["run_new", "run_old"]
        assert tx.expired_leases(T0) == []
        assert [x.id for x in tx.waiting_by_key("pr:1")] == ["w1"]
        assert tx.waiting_by_key("pr:9") == []
        assert tx.count_active() == 4  # ready, running x2, failed
        tx.update_execution("ready", status="done")
        assert tx.count_active() == 3


# -- loops, effects, blobs --------------------------------------------------------------------
def test_loop_counters(ledger: Ledger) -> None:
    seed(ledger)
    with ledger.tx() as tx:
        assert tx.loop_count("r1", "e1") == 0
        assert tx.inc_loop_count("r1", "e1") == 1
        assert tx.inc_loop_count("r1", "e1") == 2
        assert tx.inc_loop_count("r1", "e2") == 1
    with ledger.tx() as tx:
        assert (tx.loop_count("r1", "e1"), tx.loop_count("r1", "e2")) == (2, 1)
        assert tx.loop_count("r2", "e1") == 0


def test_effects_upsert(ledger: Ledger) -> None:
    seed(ledger)
    intent = Effect("r1", "a", 1, "create_pr", "intent", idempotency_key="k1", updated_at=T0)
    with ledger.tx() as tx:
        assert tx.get_effect("r1", "a", 1, "create_pr") is None
        tx.put_effect(intent)
        tx.put_effect(Effect("r1", "a", 1, "comment", "committed", result=0, updated_at=T0 + 1))
    with ledger.tx() as tx:
        assert tx.get_effect("r1", "a", 1, "create_pr") == intent
        done = Effect(
            "r1",
            "a",
            1,
            "create_pr",
            "committed",
            idempotency_key="k2",
            result={"number": 7, "url": None},
            advice={},
            updated_at=T0 + 2,
        )
        tx.put_effect(done)  # same key: replaces every field
        assert tx.get_effect("r1", "a", 1, "create_pr") == done
        assert tx.get_effect("r1", "a", 2, "create_pr") is None
        names = [(e.name, e.status, e.result) for e in tx.list_effects("r1")]
        assert names == [("comment", "committed", 0), ("create_pr", "committed", done.result)]
        redo = Effect("r1", "a", 1, "create_pr", "redo", advice={"decision": "redo"})
        tx.put_effect(redo)  # updated_at 0 -> now
        got = tx.get_effect("r1", "a", 1, "create_pr")
        assert got is not None and got.result is None and got.advice == {"decision": "redo"}
        assert got.updated_at > T0
        assert tx.list_effects("r2") == []


def test_blobs_are_content_addressed(ledger: Ledger) -> None:
    data = bytes(range(256)) * 3
    with ledger.tx() as tx:
        ref = tx.put_blob(data)
        assert ref == content_hash(data)
        assert tx.put_blob(data) == ref  # idempotent
        empty = tx.put_blob(b"")
    with ledger.tx() as tx:
        assert tx.get_blob(ref) == data
        assert tx.get_blob(empty) == b""
        assert tx.get_blob("0" * 64) is None


# -- artifacts, lineage -----------------------------------------------------------------------
def test_artifacts_and_links(ledger: Ledger) -> None:
    seed(ledger, mk_exec("e1"), mk_exec("e2", node_id="b"))
    art = Artifact("h1", "json", "application/json", 12, '{"a": 1}', "inline", None, T0)
    big = Artifact("h2", "file", "text/plain", 5_000_000_000, "big", "file", "/a/b", T0)
    with ledger.tx() as tx:
        tx.put_artifact(art)
        tx.put_artifact(Artifact("h1", "json", "x/y", 1, "other", "inline", None, T0 + 1))
        tx.put_artifact(big)
    with ledger.tx() as tx:
        assert tx.get_artifact("h1") == art  # insert-if-absent: the first row stays
        assert tx.get_artifact("h2") == big
        assert tx.get_artifact("nope") is None
    wrote = ArtifactLink("r1", "e1", "a", 1, "plan", "h1", "wrote", T0)
    read = ArtifactLink("r1", "e2", "b", 1, "plan", "h1", "read", T0 + 1, "e1")
    with ledger.tx() as tx:
        tx.add_artifact_link(wrote)
        tx.add_artifact_link(read)
        tx.add_artifact_link(ArtifactLink("r1", "e1", "a", 1, "plan", "h1", "wrote", T0 + 9))
        tx.add_artifact_link(ArtifactLink("r2", "x", "a", 1, "plan", "h1", "wrote", T0))
    with ledger.tx() as tx:
        assert tx.list_artifact_links("r1") == [wrote, read]
        assert tx.list_artifact_links("nope") == []


# -- events, deliveries, audit ----------------------------------------------------------------
def test_events_since_and_consume(ledger: Ledger) -> None:
    with ledger.tx() as tx:
        old = tx.insert_event("pr:1", {"state": "old"}, T0)
        new = tx.insert_event("pr:1", ["any", "json"], T0 + 10)
        other = tx.insert_event("pr:2", None, T0 + 5)
    assert old < new and len({old, new, other}) == 3
    with ledger.tx() as tx:
        ev = tx.unconsumed_event("pr:1")
        assert ev is not None and (ev.id, ev.payload, ev.received_at) == (old, {"state": "old"}, T0)
        ev = tx.unconsumed_event("pr:1", since=T0 + 1)
        assert ev is not None and (ev.id, ev.payload) == (new, ["any", "json"])
        assert tx.unconsumed_event("pr:1", since=T0 + 10) is not None  # since is inclusive
        assert tx.unconsumed_event("pr:1", since=T0 + 11) is None
        tx.consume_event(old, "e1")
    with ledger.tx() as tx:
        ev = tx.unconsumed_event("pr:1")
        assert ev is not None and ev.id == new and ev.consumed_by is None
        tx.consume_event(new, "e2")
        assert tx.unconsumed_event("pr:1") is None
        ev = tx.unconsumed_event("pr:2")
        assert ev is not None and ev.payload is None


def test_claim_delivery_once(ledger: Ledger) -> None:
    with ledger.tx() as tx:
        assert tx.claim_delivery("github", "d1", T0)
        assert not tx.claim_delivery("github", "d1", T0 + 1)
        assert tx.claim_delivery("event", "d1", T0)
    with pytest.raises(RuntimeError), ledger.tx() as tx:
        assert tx.claim_delivery("github", "d2", T0)
        raise RuntimeError("rollback")
    with ledger.tx() as tx:  # the rolled-back claim is gone, so a retry still works
        assert tx.claim_delivery("github", "d2", T0)


def test_audit_log(ledger: Ledger) -> None:
    with ledger.tx() as tx:
        a = tx.add_audit("huu", "approve", run_id="r1", node_id="gate", detail={"x": 1}, at=T0)
        b = tx.add_audit("bot", "deploy", run_id="r2", at=T0 + 1)
        c = tx.add_audit("huu", "cancel", run_id="r1")
    assert a < b < c
    with ledger.tx() as tx:
        r1 = tx.list_audit("r1")
        assert [(e.id, e.action, e.node_id, e.detail) for e in r1] == [
            (a, "approve", "gate", {"x": 1}),
            (c, "cancel", None, {}),
        ]
        assert r1[0].at == T0 and r1[1].at > T0
        assert [e.id for e in tx.list_audit()] == [a, b, c]
        assert [e.id for e in tx.list_audit(limit=2)] == [b, c]  # latest, oldest first
        assert [e.id for e in tx.list_audit("r1", limit=1)] == [a]
        assert tx.list_audit("nope") == []


def test_journal_and_tasks(ledger: Ledger) -> None:
    from agentline.ledger.base import TaskRecord

    with ledger.tx() as tx:
        a = tx.add_journal("r1", "status", "ready -> running", execution_id="e1", node_id="n",
                           detail={"attempt": 1}, at=T0)  # fmt: skip
        b = tx.add_journal("r1", "task", "task 2 failed", execution_id="e1", node_id="n", task=2)
        tx.add_journal("r2", "note", "other run")
        c = tx.add_journal("r1", "note", "run note")
    with ledger.tx() as tx:
        j = tx.list_journal("r1")
        assert [e.id for e in j] == [a, b, c]
        assert j[0].detail == {"attempt": 1} and j[0].at == T0 and j[1].task == 2
        assert [e.id for e in tx.list_journal("r1", node_id="n")] == [a, b]
        assert [e.id for e in tx.list_journal("r1", limit=1)] == [a]
        t = TaskRecord("e1", 0, "r1", "n", 1, "running", 1, item={"x": [1, "ü"]}, started_at=T0)
        tx.put_task(t)
        tx.put_task(TaskRecord("e1", 1, "r1", "n", 1, "pending", item="b"))
        tx.put_task(TaskRecord("e2", 0, "r1", "m", 1, "done", 1, result=[1]))
    with ledger.tx() as tx:
        t.status, t.result, t.finished_at = "done", {"ok": True}, T0 + 1
        tx.put_task(t)  # replace
    with ledger.tx() as tx:
        e1 = tx.list_tasks("r1", "e1")
        assert [(x.idx, x.status) for x in e1] == [(0, "done"), (1, "pending")]
        assert e1[0] == t and e1[1].result is None
        assert [(x.execution_id, x.idx) for x in tx.list_tasks("r1")] == [
            ("e1", 0), ("e1", 1), ("e2", 0)
        ]  # fmt: skip
        assert tx.list_tasks("nope") == []


# -- transactions -----------------------------------------------------------------------------
def test_rollback_on_exception(ledger: Ledger) -> None:
    seed(ledger, mk_exec("e1"))

    class Boom(Exception):
        pass

    with pytest.raises(Boom), ledger.tx() as tx:
        tx.insert_run(mk_run("r2"))
        tx.update_execution("e1", status="done")
        tx.inc_loop_count("r1", "loop")
        tx.put_blob(b"x")
        tx.insert_event("k", {}, T0)
        tx.add_audit("a", "b")
        raise Boom
    with ledger.tx() as tx:
        assert tx.get_run("r2") is None
        ex = tx.get_execution("e1")
        assert ex is not None and ex.status == "ready"
        assert tx.loop_count("r1", "loop") == 0
        assert tx.get_blob(content_hash(b"x")) is None
        assert tx.unconsumed_event("k") is None
        assert tx.list_audit() == []


def test_commits_are_visible_to_other_ledger_instances(backend: Backend) -> None:
    a, b = backend.open(), backend.open()
    with a.tx() as tx:
        tx.insert_run(mk_run("r1"))
    with b.tx() as tx:
        assert tx.get_run("r1") is not None
        tx.update_run("r1", status="done")
    with a.tx() as tx:
        got = tx.get_run("r1")
    assert got is not None and got.status == "done"


def test_ledger_usable_from_many_threads(ledger: Ledger) -> None:
    """The engine calls one ledger from asyncio.to_thread workers."""
    seed(ledger)
    errors: list[BaseException] = []

    def bump() -> None:
        try:
            for _ in range(10):
                with ledger.tx() as tx:
                    tx.inc_loop_count("r1", "e")
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=bump) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    with ledger.tx() as tx:
        assert tx.loop_count("r1", "e") == 60


def test_concurrent_read_modify_write_is_serialized(backend: Backend) -> None:
    """The scheduler reads, computes in Python, then writes (join tokens, run status). Two
    such transactions must not interleave, or one update is lost."""
    first = backend.open()
    seed(first, mk_exec("join", status="pending", tokens_needed=100))
    ledgers = [backend.open() for _ in range(4)]
    errors: list[BaseException] = []

    def add_tokens(ledger: Ledger) -> None:
        try:
            for _ in range(10):
                with ledger.tx() as tx:
                    ex = tx.get_execution("join")
                    assert ex is not None
                    tx.update_execution("join", tokens_live=ex.tokens_live + 1)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=add_tokens, args=(lg,)) for lg in ledgers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    with first.tx() as tx:
        ex = tx.get_execution("join")
    assert ex is not None and ex.tokens_live == 40


# -- the engine on each backend ---------------------------------------------------------------
def test_engine_end_to_end(ledger: Ledger) -> None:
    app = App(ledger, backoff_base=0.0)
    flow = app.flow("t")
    calls: list[str] = []

    @flow.step("plan")
    async def plan(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"ticket": inputs["payload"]["ticket"]}

    @flow.step("left", after="plan")
    async def left(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        await ctx.effect("post", lambda: _record(calls, "left"))
        return {"side": "left"}

    @flow.step("right", after="plan")
    async def right(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        if ctx.attempt == 1:
            raise RuntimeError("flaky")
        return {"side": "right"}

    @flow.step("gate", after=["left", "right"])
    async def gate(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        return ctx.wait(f"gate:{ctx.run_id}", prompt="ok?")

    @flow.step("deploy", after="gate")
    async def deploy(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"by": inputs["gate"]["by"]}

    subs = [app.submit_sync("t", {"ticket": f"T-{i}"}, dedupe_key=f"k{i}") for i in range(6)]
    again = app.submit_sync("t", {"ticket": "T-0"}, dedupe_key="k0")
    assert again.run_id == subs[0].run_id
    asyncio.run(app.worker(concurrency=4).run_until_idle())
    for s in subs:
        assert app.get_run(s.run_id).status == RunStatus.WAITING  # type: ignore[union-attr]
        assert asyncio.run(app.deliver_event(f"gate:{s.run_id}", {"by": "huu"}))
    asyncio.run(app.worker(concurrency=4).run_until_idle())
    for s in subs:
        assert app.get_run(s.run_id).status == RunStatus.DONE  # type: ignore[union-attr]
        assert app.result(s.run_id, "deploy") == {"by": "huu"}
    assert calls == ["left"] * 6


async def _record(calls: list[str], name: str) -> dict[str, str]:
    calls.append(name)
    return {"ok": name}


def test_run_messages(ledger: Ledger) -> None:
    with ledger.tx() as tx:
        a = tx.add_run_message("r1", "user", "fix the login bug ü", via="app", at=T0)
        tx.add_run_message("r2", "user", "other run")
        b = tx.add_run_message("r1", "agent", "which repo?", via="intake",
                               detail={"action": "ask"})  # fmt: skip
        c = tx.add_run_message("r1", "system", "qa failed")
    with ledger.tx() as tx:
        m = tx.list_run_messages("r1")
        assert [x.id for x in m] == [a, b, c] and m[0].text == "fix the login bug ü"
        assert m[0].at == T0 and m[0].via == "app" and m[1].detail == {"action": "ask"}
        assert [x.id for x in tx.list_run_messages("r1", after=a)] == [b, c]
        assert [x.id for x in tx.list_run_messages("r1", after=a, limit=1)] == [b]
        tx.update_run_message(b, {"action": "ask", "actions": [{"type": "retry"}]})
    with ledger.tx() as tx:
        assert tx.list_run_messages("r1", after=a)[0].detail["actions"] == [{"type": "retry"}]
        assert tx.list_run_messages("nope") == []
