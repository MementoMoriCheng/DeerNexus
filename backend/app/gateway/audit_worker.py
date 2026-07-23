"""Background outbox-draining worker (PR-041).

The worker is the delivery half of the transactional outbox (ADR-0005 §8):
it repeatedly claims ``pending`` rows from ``audit_outbox``, publishes each to
the append-only ``audit_events`` store (PR-040's ``insert_audit_event``), and
marks the outbox row ``published`` — or, on failure, applies exponential
backoff and eventually dead-letters the row.

Lifecycle: the gateway lifespan starts exactly one worker per process via
``run_audit_worker`` (an ``asyncio.create_task``), capturing the session
factory while the engine is live, and cancels it on shutdown bounded by
``_SHUTDOWN_HOOK_TIMEOUT_SECONDS`` (mirroring the channel-service stop pattern
in ``app.gateway.app``). ``drain_audit_outbox`` is the single-pass pure-ish
function the worker loop calls — it is also the unit-test entry point (no loop,
no sleep, no task).

Idempotency (ADR §9.1): publishing a row whose ``event_id`` already exists in
``audit_events`` raises ``IntegrityError`` — that is the success path (a prior
attempt published it before crashing), so the worker marks the outbox row
``published`` rather than retrying. A duplicate publish therefore produces one
``audit_events`` row, never two.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from deerflow.contracts.events import AuditEvent
from deerflow.persistence.audit.outbox import (
    DEAD_LETTER_THRESHOLD,
    OUTBOX_DEAD_LETTER,
    OUTBOX_PENDING,
    claim_audit_outbox,
    count_dead_letter,
    count_pending,
    mark_outbox_failed,
    mark_outbox_published,
    oldest_pending_age_seconds,
    release_stale_processing,
)
from deerflow.persistence.audit.repository import insert_audit_event

logger = logging.getLogger(__name__)

#: How many rows a single drain claims per pass (ADR §8 "pending 按
#: available_at 领取"). Bounded so one worker pass is short and cancellable.
CLAIM_BATCH_SIZE = 50

#: Idle interval between drain passes when there is nothing to do. The worker
#: wakes at most this often even when the queue is empty, so newly-enqueued
#: rows are picked up within this latency. Must be << the P99 publish SLO.
WORKER_INTERVAL_SECONDS = 5.0


def _new_owner_token() -> str:
    """Per-process, per-drain token identifying the claiming worker."""
    return uuid.uuid4().hex


async def drain_audit_outbox(
    sf: async_sessionmaker,
    *,
    now: datetime | None = None,
    batch_size: int = CLAIM_BATCH_SIZE,
) -> int:
    """Drain one pass of the outbox: reconcile → claim → publish each row.

    Returns the number of rows published this pass. Pure-ish: no loop, no
    sleep, no background task — the worker loop (``run_audit_worker``) calls
    this repeatedly. This separation makes the delivery logic unit-testable.

    Per pass:
    1. release stale ``processing`` rows (a crashed worker's orphans);
    2. claim up to ``batch_size`` ``pending`` rows atomically;
    3. for each: deserialize the stored ``AuditEvent`` and ``insert_audit_event``
       — success OR an ``IntegrityError`` (already published) → mark published;
       any other failure → mark failed (backoff, maybe dead-letter).

    Observability (ADR §14): pending count, oldest age, and dead-letter count
    are read once per pass and pushed to the metrics registry (fail-open).
    """
    if now is None:
        now = datetime.now(UTC)
    owner_token = _new_owner_token()

    # 1. Reconcile: release orphaned processing rows from a crashed worker.
    try:
        await release_stale_processing(sf, now=now)
    except Exception:  # noqa: BLE001
        logger.warning("audit outbox stale-release failed; continuing with claim", exc_info=True)

    # 2. Claim.
    try:
        claimed = await claim_audit_outbox(sf, batch_size=batch_size, owner_token=owner_token, now=now)
    except Exception:  # noqa: BLE001
        logger.warning("audit outbox claim failed; aborting drain pass", exc_info=True)
        return 0

    published = 0
    # 3. Publish each claimed row.
    for row in claimed:
        try:
            event = AuditEvent.model_validate_json(row.payload_json)
        except Exception:  # noqa: BLE001
            # Undeserialisable payload — the row can never be published; treat
            # as a hard failure (will back off then dead-letter).
            logger.error("audit outbox row %s payload undecodable; marking failed", row.id, exc_info=True)
            await _safe_mark_failed(sf, row.id, "payload undecodable", now)
            continue

        try:
            await insert_audit_event(sf, event, producer="audit-outbox-worker")
            await mark_outbox_published(sf, row_id=row.id, now=now)
            published += 1
        except IntegrityError:
            # event_id already in audit_events → a prior attempt published it
            # before crashing. Idempotent success: mark the outbox row done.
            await mark_outbox_published(sf, row_id=row.id, now=now)
            published += 1
        except Exception as exc:  # noqa: BLE001
            new_status = await _safe_mark_failed(sf, row.id, exc, now)
            if new_status == OUTBOX_DEAD_LETTER:
                logger.error(
                    "audit outbox row %s (event_id=%s) reached dead-letter after %d attempts",
                    row.id,
                    row.event_id,
                    DEAD_LETTER_THRESHOLD,
                )

    # 4. Observability snapshot (ADR §14). Fail-open: never break the worker.
    await _publish_backlog_metrics(sf, now)
    return published


async def _safe_mark_failed(sf: async_sessionmaker, row_id: str, error: object, now: datetime) -> str:
    """Mark a row failed, swallowing any DB error so the drain continues."""
    try:
        return await mark_outbox_failed(sf, row_id=row_id, error=error, now=now)
    except Exception:  # noqa: BLE001
        logger.error("audit outbox mark-failed for row %s itself failed", row_id, exc_info=True)
        return OUTBOX_PENDING


async def _publish_backlog_metrics(sf: async_sessionmaker, now: datetime) -> None:
    """Push the ADR §14 outbox gauges (fail-open)."""
    try:
        from deerflow.observability import metrics

        pending = await count_pending(sf, now=now)
        age = await oldest_pending_age_seconds(sf, now=now)
        dead = await count_dead_letter(sf)
        metrics.set_audit_outbox_pending(pending)
        metrics.set_audit_outbox_oldest_age(age)
        if dead:
            metrics.set_audit_dead_letter_count(dead)
    except Exception:  # noqa: BLE001
        logger.debug("audit outbox metric snapshot failed", exc_info=True)


async def run_audit_worker(
    sf: async_sessionmaker,
    *,
    interval: float = WORKER_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop: drain the outbox every ``interval`` until ``stop_event``.

    Started as an ``asyncio.create_task`` in the gateway lifespan; the lifespan
    sets ``stop_event`` and awaits the task (bounded) on shutdown. Each pass is
    a full ``drain_audit_outbox``; the loop sleeps ``interval`` between passes,
    interruptible by ``stop_event`` so shutdown is prompt even mid-idle.
    """
    if stop_event is None:
        stop_event = asyncio.Event()
    logger.info("audit outbox worker started (interval=%.1fs, batch=%d)", interval, CLAIM_BATCH_SIZE)
    while not stop_event.is_set():
        try:
            await drain_audit_outbox(sf)
        except Exception:  # noqa: BLE001
            # A drain pass must never kill the worker — log and continue.
            logger.exception("audit outbox drain pass raised; continuing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass  # interval elapsed; loop and drain again
    logger.info("audit outbox worker stopped")


__all__ = [
    "CLAIM_BATCH_SIZE",
    "WORKER_INTERVAL_SECONDS",
    "drain_audit_outbox",
    "run_audit_worker",
]
