"""Background reconciler that converges orphaned non-terminal runs (PR-072, Track G).

PR-070 added the run state CAS (terminal immutability); PR-071 added the
ownership/lease layer. This module is the periodic sweep that uses both to
detect non-terminal runs whose owner is gone (lease expired / no holder, or no
local in-memory task in single-worker mode) and converge them to a safe
terminal state via the CAS — **without replaying** the run (TM-028 Critical:
side-effects of unknown outcome are never auto-retried; the reconciler only
makes the ambiguous non-terminal state explicit, emitting a
``run.reconcile.result`` event so an operator can decide on manual follow-up).

The sweep mirrors the resilience contract of ``run_audit_worker`` /
``run_release_gc_worker``: a failing pass is logged and retried next interval,
never fatal; ``stop_event`` breaks the idle sleep for prompt shutdown.

PG is authoritative (ADR-0006 §5.3): the reconciler reads PG terminal state
first and never revives a committed terminal run; a CAS miss (a concurrent
writer already moved the row) leaves the row untouched.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deerflow.runtime.runs.manager import RunManager

logger = logging.getLogger(__name__)

#: How often the reconciler scans non-terminal runs. Picked to be a few × the
#: lease TTL (PR-071 ``LEASE_TTL_SECONDS=30``) so an expired-lease orphan is
#: converged well within a reasonable recovery window without burning CPU on an
#: idle system. Lower for faster recovery at higher scan cost.
RECONCILE_INTERVAL_SECONDS: float = 60.0

#: The terminal-state reason written when the reconciler reclaims an orphan.
#: Surfaces in the run row's ``error`` column and the ``run.reconcile.result``
#: event content so an operator understands why the run was converged.
RECLAIM_REASON: str = "Run owner is gone (lease expired or no holder); converged to a safe terminal by the reconciler."


async def sweep_inflight_runs(
    run_manager: RunManager,
    *,
    lease_store: Any = None,
    run_event_store: Any = None,
    reason: str = RECLAIM_REASON,
) -> int:
    """Run one reconcile pass. Returns the number of runs reclaimed.

    Thin wrapper over ``RunManager.reconcile_orphaned_inflight_runs`` so the
    background loop has a single-call pass + a return value for logging. The
    lease/event stores are forwarded so the lease-aware + observable paths fire.
    """
    recovered = await run_manager.reconcile_orphaned_inflight_runs(
        error=reason,
        lease_store=lease_store,
        run_event_store=run_event_store,
    )
    if recovered:
        logger.info("reconcile sweep reclaimed %d orphaned run(s)", len(recovered))
    return len(recovered)


async def run_reconcile_worker(
    run_manager: RunManager,
    *,
    lease_store: Any = None,
    run_event_store: Any = None,
    interval: float = RECONCILE_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop: reconcile orphaned non-terminal runs every ``interval``.

    Started as an ``asyncio.create_task`` in the gateway lifespan (alongside
    the audit outbox worker and the release GC worker); the lifespan sets
    ``stop_event`` and awaits the task on shutdown. Each pass is a full
    ``sweep_inflight_runs``; the loop sleeps ``interval`` between passes,
    interruptible by ``stop_event`` so shutdown is prompt even mid-idle.
    """
    if stop_event is None:
        stop_event = asyncio.Event()
    logger.info("run reconciler worker started (interval=%.0fs)", interval)
    while not stop_event.is_set():
        try:
            await sweep_inflight_runs(run_manager, lease_store=lease_store, run_event_store=run_event_store)
        except Exception:  # noqa: BLE001
            # A sweep pass must never kill the worker — log and continue.
            logger.exception("reconcile sweep raised; continuing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass  # interval elapsed; loop and sweep again
    logger.info("run reconciler worker stopped")


__all__ = [
    "RECLAIM_REASON",
    "RECONCILE_INTERVAL_SECONDS",
    "run_reconcile_worker",
    "sweep_inflight_runs",
]
