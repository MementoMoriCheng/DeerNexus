"""Background sweep that prunes stale Idempotency-Key replay records.

PR-055 added the ``release_idempotency_records`` replay store (full-response
replay for promote/rollback retries). Replay records are self-contained (no FK)
and otherwise accumulate indefinitely; §16.56 flagged a TTL/prune sweep keyed on
``created_at`` as a follow-up. This module is that follow-up.

A replay record older than the retention window is no longer useful: an
``Idempotency-Key`` retry that long after the original is not a legitimate
client replay of the same logical request, so pruning cannot break the replay
contract. The GC is best-effort and independent of request traffic — a sweep
failure is logged and retried next interval, never fatal (mirrors
``run_audit_worker``'s resilience shape).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from deerflow.persistence.release import delete_idempotency_records_older_than

logger = logging.getLogger(__name__)

#: Retention window. A replay record older than this is pruned. 30 days matches
#: the realistic upper bound on a legitimate client retry of the same logical
#: promote/rollback — well beyond any sensible retry storm window while keeping
#: the table bounded.
GC_RETENTION_DAYS = 30

#: Idle interval between sweep passes. Replay-record growth is low volume
#: (one row per successful promote/rollback, gated by client retries), so a
#: daily cadence keeps the table bounded without measurable load. Must be
#: far less than the retention window or the table can grow between sweeps.
SWEEP_INTERVAL_SECONDS = 24 * 60 * 60.0  # once per day


async def sweep_release_idempotency_records(
    sf: async_sessionmaker,
    *,
    retention_days: int = GC_RETENTION_DAYS,
    now: datetime | None = None,
) -> int:
    """Prune replay records older than the retention window. Returns rows removed.

    Single pass, idempotent. ``now`` is injectable for deterministic tests; in
    production it is ``datetime.now(UTC)``. The cutoff is
    ``now - retention_days``.
    """
    if now is None:
        now = datetime.now(UTC)
    cutoff = now - timedelta(days=retention_days)
    removed = await delete_idempotency_records_older_than(sf, cutoff=cutoff)
    if removed:
        logger.info(
            "release_idempotency GC: pruned %d replay records older than %s (%d-day window)",
            removed,
            cutoff.isoformat(),
            retention_days,
        )
    return removed


async def run_release_gc_worker(
    sf: async_sessionmaker,
    *,
    interval: float = SWEEP_INTERVAL_SECONDS,
    retention_days: int = GC_RETENTION_DAYS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop: prune stale replay records every ``interval`` until ``stop_event``.

    Started as an ``asyncio.create_task`` in the gateway lifespan (alongside
    ``run_audit_worker``); the lifespan sets ``stop_event`` and awaits the task
    on shutdown. Each pass prunes rows older than the retention window; a pass
    that raises is logged and the loop continues (GC must never kill the worker).
    """
    if stop_event is None:
        stop_event = asyncio.Event()
    logger.info(
        "release idempotency GC worker started (interval=%.0fs, retention=%dd)",
        interval,
        retention_days,
    )
    while not stop_event.is_set():
        try:
            await sweep_release_idempotency_records(sf, retention_days=retention_days)
        except Exception:  # noqa: BLE001
            # A sweep pass must never kill the worker — log and continue.
            logger.exception("release idempotency GC sweep raised; continuing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass  # interval elapsed; loop and sweep again
    logger.info("release idempotency GC worker stopped")


__all__ = [
    "GC_RETENTION_DAYS",
    "SWEEP_INTERVAL_SECONDS",
    "run_release_gc_worker",
    "sweep_release_idempotency_records",
]
