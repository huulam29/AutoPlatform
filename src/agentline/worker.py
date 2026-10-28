"""Worker: claim ready executions under a lease, heartbeat while they run, commit results."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from typing import TYPE_CHECKING, Any

from agentline.context import StepContext
from agentline.errors import NeedsReview, PermanentError, StaleLease
from agentline.ids import new_id
from agentline.incidents import is_bug
from agentline.ledger.base import Execution
from agentline.results import Route, Wait
from agentline.scheduler import Fence
from agentline.states import ExecStatus

if TYPE_CHECKING:
    from agentline.app import App

log = logging.getLogger("agentline.worker")


class Worker:
    def __init__(
        self,
        app: App,
        *,
        worker_id: str | None = None,
        concurrency: int = 4,
        lease_ttl: float = 30.0,
        poll_interval: float = 0.1,
    ) -> None:
        self.app = app
        self.id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{new_id()[-6:]}"
        self.concurrency = concurrency
        self.lease_ttl = lease_ttl
        self.heartbeat_interval = lease_ttl / 3
        self.poll_interval = poll_interval
        self._in_flight = 0

    async def run(self, stop: asyncio.Event | None = None, *, until_idle: bool = False) -> None:
        """Run `concurrency` slots until `stop` is set, or with until_idle, until no execution
        is ready, running or backing off (waiting/needs-review runs need an outside event)."""
        stop = stop or asyncio.Event()
        reaper = asyncio.create_task(self._reaper(stop))
        try:
            await asyncio.gather(*(self._slot(stop, until_idle) for _ in range(self.concurrency)))
        finally:
            stop.set()
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper

    async def run_until_idle(self) -> None:
        await self.run(until_idle=True)

    def _report(self, exc: BaseException, **context: Any) -> None:
        if self.app.incidents is not None:
            self.app.incidents.record_exception(
                "worker_error", exc, context={"worker": self.id, **context}
            )

    async def _slot(self, stop: asyncio.Event, until_idle: bool) -> None:
        while not stop.is_set():
            try:
                did = await self.tick()
            except Exception as e:  # e.g. the ledger was locked or the disk is full
                log.exception("worker loop error (continuing): %s", _err(e))
                self._report(e, where="worker loop")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), 1.0)
                continue
            if did:
                continue
            if until_idle and self._in_flight == 0 and await asyncio.to_thread(self._idle):
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), self.poll_interval)

    async def _reaper(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(self._reclaim)
            except Exception as e:
                log.exception("lease reaper error (continuing): %s", _err(e))
                self._report(e, where="lease reaper")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), max(0.05, self.lease_ttl / 4))

    def _idle(self) -> bool:
        """Nothing ready, running (anywhere) or backing off: no work can appear by itself."""
        with self.app.ledger.tx() as tx:
            return tx.count_active() == 0

    def _reclaim(self) -> None:
        with self.app.ledger.tx() as tx:
            if ids := self.app.scheduler.reclaim_expired(tx):
                log.warning("reclaimed %d expired lease(s): %s", len(ids), ids)

    async def tick(self) -> bool:
        """Claim and execute one execution. Returns False if nothing was claimable."""
        ex = await asyncio.to_thread(self._claim)
        if ex is None:
            return False
        self._in_flight += 1
        try:
            await self._execute(ex)
        finally:
            self._in_flight -= 1
        return True

    def _claim(self) -> Execution | None:
        with self.app.ledger.tx() as tx:
            return tx.claim_next(self.id, self.app.scheduler.clock(), self.lease_ttl)

    async def _execute(self, ex: Execution) -> None:
        sched = self.app.scheduler
        fence = Fence(ExecStatus.RUNNING, ex.lease_epoch)

        def prepare() -> tuple[Any, dict[str, Any]]:
            with self.app.ledger.tx() as tx:
                run = tx.get_run(ex.run_id)
                assert run is not None
                return run, sched.resolve_inputs(tx, ex)

        try:
            run, inputs = await asyncio.to_thread(prepare)
        except PermanentError as e:
            msg = str(e)
            await self._commit(lambda tx: sched.fail(tx, ex, msg, retryable=False, fence=fence))
            return

        node = self.app.graph(run.flow, run).nodes[ex.node_id]
        log.debug("%s: claimed (attempt %d)", ex.key, ex.attempt, extra=_ctx(ex, self.id))
        ctx = StepContext(
            app=self.app,
            execution=ex,
            payload=run.payload,
            worker_id=self.id,
            node_budget_usd=node.budget_usd,
        )
        handler_task = asyncio.create_task(self._call(node.handler, ctx, inputs, node.timeout))
        lost = asyncio.Event()
        hb = asyncio.create_task(self._heartbeat(ex, handler_task, lost))
        try:
            result = await handler_task
        except NeedsReview as e:
            reason, detail, msg = e.reason, {**ctx.notes, **e.detail}, str(e)
            await self._commit(
                lambda tx: sched.needs_review(tx, ex, reason, detail, fence, error=msg)
            )
        except PermanentError as e:
            msg = _err(e)
            await self._commit(lambda tx: sched.fail(tx, ex, msg, retryable=False, fence=fence))
        except StaleLease as e:
            log.warning("%s: %s", ex.key, e, extra=_ctx(ex, self.id))
        except asyncio.CancelledError:
            if not lost.is_set():
                raise
            log.warning("%s: lease lost, handler cancelled", ex.key, extra=_ctx(ex, self.id))
        except Exception as e:
            msg = _err(e)
            log.warning(
                "%s attempt %d failed: %s", ex.key, ex.attempt, msg, extra=_ctx(ex, self.id)
            )
            if is_bug(e):  # a programming error, not a step failing for a known reason
                self._report(e, where="step", run_id=ex.run_id, node=ex.key, attempt=ex.attempt)
            await self._commit(lambda tx: sched.fail(tx, ex, msg, retryable=True, fence=fence))
        else:
            await self._finish(ex, result, fence, ctx.notes)
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb

    async def _finish(
        self, ex: Execution, result: Any, fence: Fence, notes: dict[str, Any]
    ) -> None:
        sched = self.app.scheduler
        if isinstance(result, Wait):
            key, detail = result.correlation_key, {**notes, **result.detail}
            await self._commit(lambda tx: sched.park(tx, ex, key, detail, fence))
            return
        if isinstance(result, Route):
            result = result.as_result()
        try:
            await self._commit(lambda tx: sched.complete(tx, ex, result, fence, notes or None))
        except PermanentError as e:  # e.g. router chose a label with no edge
            msg = str(e)
            await self._commit(lambda tx: sched.fail(tx, ex, msg, retryable=False, fence=fence))

    @staticmethod
    async def _call(handler: Any, ctx: StepContext, inputs: dict[str, Any], timeout: Any) -> Any:
        if timeout:
            return await asyncio.wait_for(handler(ctx, inputs), timeout)
        return await handler(ctx, inputs)

    async def _commit(self, fn: Any) -> None:
        def _do() -> None:
            with self.app.ledger.tx() as tx:
                fn(tx)

        try:
            await asyncio.to_thread(_do)
        except StaleLease as e:
            log.warning("commit rejected, lease lost: %s", e)

    async def _heartbeat(self, ex: Execution, task: asyncio.Task[Any], lost: asyncio.Event) -> None:
        while not task.done():
            await asyncio.sleep(self.heartbeat_interval)

            def beat() -> bool:
                with self.app.ledger.tx() as tx:
                    return self.app.scheduler.heartbeat(tx, ex, self.lease_ttl)

            if not await asyncio.to_thread(beat):
                lost.set()
                task.cancel()
                return


def _ctx(ex: Execution, worker_id: str) -> dict[str, Any]:
    """Log-record context (agentline.logs puts these fields on every JSON line)."""
    return {
        "run_id": ex.run_id,
        "node_id": ex.node_id,
        "iteration": ex.iteration,
        "execution_id": ex.id,
        "worker_id": worker_id,
    }


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"
