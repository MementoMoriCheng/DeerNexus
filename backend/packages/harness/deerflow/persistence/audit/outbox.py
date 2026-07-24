"""Transactional outbox repository for audit events (PR-041).

This module owns the **queue lifecycle** of the ``audit_outbox`` table
(ADR-0005 §8): enqueue, atomic claim, publish, fail-with-backoff, dead-letter,
and stale-``processing`` release. It is distinct from
``persistence/audit/repository.py`` (the append-only ``audit_events`` INSERT +
SELECT surface): the outbox row has a legitimate status transition
(``pending → processing → published | dead_letter``), so it carries the
mutation helpers here rather than in the immutable store.

Reliability contract (ADR §7.1 / §8 / §9.1):

* a Class A control-plane write enqueues an outbox row in the **same
  transaction** as the business change (§7.1 — the enqueue must fail-rollback
  the business write; the router passes its own session to
  :func:`enqueue_audit_outbox_in_session` and commits both atomically);
* delivery is idempotent by ``event_id`` (§9.1): the unique index
  ``uq_audit_outbox_event_id`` means a replay that re-enqueues the same
  ``event_id`` raises ``IntegrityError`` rather than queueing a duplicate, and
  a worker that re-publishes after a crash finds ``audit_events`` already has
  the row and marks the outbox row ``published`` without a second insert;
* failed publishes use **exponential backoff with a max interval**, and after
  the threshold the row enters ``dead_letter`` (P2 alert);
* a Reconciler can re-release ``processing`` rows that have exceeded the
  stale window (a worker crashed mid-publish).

Claim concurrency (ADR §8 "领取使用原子 claim")
------------------------------------------------

The claim must be atomic across concurrent workers. The strategy is
dialect-aware, mirroring ``runtime/events/store/db.py``'s advisory-lock
approach:

* **Postgres:** ``SELECT ... FOR UPDATE SKIP LOCKED LIMIT N`` — row-level
  locks, multiple workers drain disjoint batches.
* **SQLite:** single writer per file (WAL); ``FOR UPDATE`` is a no-op and
  ``SKIP LOCKED`` is a syntax error. The claim is a single conditional
  ``UPDATE ... WHERE status='pending' AND available_at <= :now RETURNING *``:
  under the process-wide write lock the first worker flips its batch to
  ``processing`` and the second worker's identical ``UPDATE`` finds those rows
  already ``processing`` (0 rows affected). This is atomic at the statement
  level (SQLite ≥ 3.35 supports ``RETURNING``).

In practice the gateway runs one worker per process (PR-041 starts exactly
one), so contention is rare; the locking is defence-in-depth for the
multi-worker / multi-process future (and for Postgres prod).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.contracts.events import AuditEvent
from deerflow.persistence.audit.model import AuditOutboxRow

#: Outbox status values allowed by the ``ck_audit_outbox_status`` CHECK.
OUTBOX_PENDING = "pending"
OUTBOX_PROCESSING = "processing"
OUTBOX_PUBLISHED = "published"
OUTBOX_DEAD_LETTER = "dead_letter"
_ALLOWED_STATUSES: frozenset[str] = frozenset({OUTBOX_PENDING, OUTBOX_PROCESSING, OUTBOX_PUBLISHED, OUTBOX_DEAD_LETTER})

#: Exponential backoff for failed publishes (ADR §8 "失败指数退避并有最大间隔").
#: ``available_at`` is pushed forward by ``min(BASE * 2^attempts, MAX)``.
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 300.0

#: After this many attempts a row enters ``dead_letter`` (ADR §8 "达到重试
#: 阈值进入 dead_letter"). Each failure increments ``attempts`` first, so the
#: transition fires when ``attempts`` reaches this threshold.
DEAD_LETTER_THRESHOLD = 10

#: A ``processing`` row older than this is assumed orphaned (worker crashed
#: mid-publish) and is released back to ``pending`` by the reconciler.
STALE_PROCESSING_SECONDS = 300.0


def _new_id() -> str:
    """Generate a 36-char hex id matching the ``String(36)`` convention."""
    return uuid.uuid4().hex


def _backoff_for(attempts: int) -> float:
    """Exponential backoff seconds for a given attempt count (clamped to MAX).

    ``attempts`` is the count *after* incrementing on this failure. attempt 1
    → 2s, 2 → 4s, … clamped at ``BACKOFF_MAX_SECONDS``.
    """
    if attempts <= 0:
        return BACKOFF_BASE_SECONDS
    return min(BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), BACKOFF_MAX_SECONDS)


def _truncate_error(error: object, *, limit: int = 512) -> str:
    """Render ``error`` into a bounded, secret-free ``last_error`` string.

    ADR §8 "不在 last_error 保存 Secret": the worker never stores a raw
    exception that might carry a credential. We render the type + message and
    hard-truncate; the underlying ``AuditEvent`` payload is already scrubbed by
    ``contracts.events._scrub_payload`` at enqueue, so there is no payload
    secret to leak here — this guard is for exception text (e.g. a DB driver
    error echoing a DSN).
    """
    if isinstance(error, BaseException):
        rendered = f"{type(error).__name__}: {error}"
    else:
        rendered = str(error)
    if len(rendered) > limit:
        rendered = rendered[:limit]
    return rendered


def _as_utc(value: datetime) -> datetime:
    """Coerce a DB-returned timestamp to timezone-aware UTC.

    SQLite strips ``tzinfo`` on round-trip (``DateTime(timezone=True)`` is
    declarative on SQLite), so a freshly-read ``available_at`` / ``updated_at``
    is offset-naive and cannot be subtracted from ``datetime.now(UTC)``.

    The value was *written* in UTC (every ``now`` default here is
    ``datetime.now(UTC)``), so the naive value IS already UTC and we stamp it
    with ``replace(tzinfo=UTC)`` — NOT ``astimezone(UTC)``, which would
    reinterpret the naive value as *local* time and shift it by the host's
    UTC offset. Postgres preserves tzinfo and this branch is skipped there.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


async def enqueue_audit_outbox(
    sf: async_sessionmaker[AsyncSession],
    event: AuditEvent,
    *,
    now: datetime | None = None,
) -> AuditOutboxRow:
    """Persist one ``AuditEvent`` as a ``pending`` outbox row (ADR §7.1/§8).

    The full event is serialised (``model_dump_json``) into ``payload_json`` so
    the worker can reconstruct and publish it with no cross-table JOIN. The
    unique index ``uq_audit_outbox_event_id`` makes a replay collide on
    ``event_id`` → ``IntegrityError`` (idempotent by event_id, §9.1).

    For the Class A same-transaction path (§7.1 — the enqueue must fail-rollback
    the business write) use :func:`enqueue_audit_outbox_in_session`, which adds
    the row to a caller-owned session without committing; this helper opens its
    own session for the post-commit path the shim upgrade (PR-041) uses.
    """
    if now is None:
        now = datetime.now(UTC)
    row = AuditOutboxRow(
        id=_new_id(),
        event_id=event.event_id,
        payload_json=event.model_dump_json(),
        org_id=event.org_id,
        status=OUTBOX_PENDING,
        attempts=0,
        available_at=now,
        created_at=now,
        updated_at=now,
    )
    async with sf() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def enqueue_audit_outbox_in_session(
    session: AsyncSession,
    event: AuditEvent,
    *,
    now: datetime | None = None,
) -> AuditOutboxRow:
    """Same-transaction enqueue: add a ``pending`` row to ``session`` (ADR §7.1).

    The caller OWNS the transaction: this helper only stages the row
    (``session.add``) and flushes so the unique-index collision surfaces inside
    this scope. It does **not** commit — the caller's ``session.commit()``
    atomically lands both the business write and the outbox row, or rolls back
    both on failure (§7.1 "outbox 写失败则业务回滚"). Returned row is attached
    to the caller's session; ``session.refresh`` is the caller's concern post-commit.

    Idempotency (§9.1) is unchanged: a replay enqueuing the same ``event_id``
    raises ``IntegrityError`` at flush on the ``uq_audit_outbox_event_id`` index
    — which aborts the *shared* transaction and thus the business write too
    (exactly the §7.1 fail-rollback contract, since a duplicate event_id means
    the caller is re-driving an already-recorded mutation).
    """
    if now is None:
        now = datetime.now(UTC)
    row = AuditOutboxRow(
        id=_new_id(),
        event_id=event.event_id,
        payload_json=event.model_dump_json(),
        org_id=event.org_id,
        status=OUTBOX_PENDING,
        attempts=0,
        available_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()  # surface unique-index collision inside this scope
    return row


# ---------------------------------------------------------------------------
# Claim (atomic, dialect-aware)
# ---------------------------------------------------------------------------


async def claim_audit_outbox(
    sf: async_sessionmaker[AsyncSession],
    *,
    batch_size: int,
    owner_token: str,
    now: datetime | None = None,
) -> list[AuditOutboxRow]:
    """Atomically claim up to ``batch_size`` pending rows → ``processing``.

    ADR §8 "领取使用原子 claim". Returns the claimed rows (already flipped to
    ``processing`` and stamped with ``owner_token``). Concurrent callers never
    receive overlapping rows — see the module docstring for the dialect-aware
    locking strategy.

    Only rows with ``status='pending' AND available_at <= now`` are eligible
    (backoff-pushed rows are skipped until their ``available_at``).
    """
    if now is None:
        now = datetime.now(UTC)
    if batch_size <= 0:
        return []

    async with sf() as session:
        dialect = session.bind.dialect.name  # type: ignore[union-attr]
        if dialect == "postgresql":
            claimed = await _claim_postgres(session, batch_size=batch_size, owner_token=owner_token, now=now)
        else:
            claimed = await _claim_sqlite(session, batch_size=batch_size, owner_token=owner_token, now=now)
        await session.commit()
        return claimed


async def _claim_postgres(
    session: AsyncSession,
    *,
    batch_size: int,
    owner_token: str,
    now: datetime,
) -> list[AuditOutboxRow]:
    """Postgres claim via ``FOR UPDATE SKIP LOCKED`` (ADR §8)."""
    stmt = select(AuditOutboxRow).where(AuditOutboxRow.status == OUTBOX_PENDING, AuditOutboxRow.available_at <= now).order_by(AuditOutboxRow.available_at.asc(), AuditOutboxRow.id.asc()).limit(batch_size).with_for_update(skip_locked=True)
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        row.status = OUTBOX_PROCESSING
        row.owner_token = owner_token
        row.updated_at = now
    return rows


async def _claim_sqlite(
    session: AsyncSession,
    *,
    batch_size: int,
    owner_token: str,
    now: datetime,
) -> list[AuditOutboxRow]:
    """SQLite claim: SELECT bounded ids, then UPDATE exactly those → processing.

    SQLite has no ``LIMIT`` on ``UPDATE`` and no row-level locks, so a single
    ``UPDATE ... WHERE status='pending' RETURNING *`` would flip the *entire*
    backlog to ``processing`` regardless of ``batch_size`` (and starve other
    workers). The correct atomic shape under SQLite's single-writer model is:

    1. SELECT the bounded candidate ids (``LIMIT batch_size``) — under the
       write lock this snapshot is stable for the immediately-following UPDATE.
    2. UPDATE exactly those ids to ``processing`` (``WHERE id IN (...)``) and
       RETURNING them, so the returned rows reflect the flipped state.

    A concurrent worker's step-1 SELECT cannot observe the same pending rows
    once step-2 has committed (single writer), so two claims never overlap.
    """
    id_stmt = select(AuditOutboxRow.id).where(AuditOutboxRow.status == OUTBOX_PENDING, AuditOutboxRow.available_at <= now).order_by(AuditOutboxRow.available_at.asc(), AuditOutboxRow.id.asc()).limit(batch_size)
    ids = [row for row in (await session.execute(id_stmt)).scalars().all()]
    if not ids:
        return []
    update_stmt = update(AuditOutboxRow).where(AuditOutboxRow.id.in_(ids)).values(status=OUTBOX_PROCESSING, owner_token=owner_token, updated_at=now).execution_options(synchronize_session=False).returning(AuditOutboxRow)
    rows = list((await session.execute(update_stmt)).scalars().all())
    # Preserve the available_at/id order from the SELECT (RETURNING is unordered).
    order = {rid: i for i, rid in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r.id, len(ids)))
    return rows


# ---------------------------------------------------------------------------
# Publish / fail / dead-letter
# ---------------------------------------------------------------------------


async def mark_outbox_published(
    sf: async_sessionmaker[AsyncSession],
    *,
    row_id: str,
    now: datetime | None = None,
) -> None:
    """Mark a claimed row ``published`` and stamp ``published_at`` (ADR §8).

    Idempotent: the worker calls this after a successful
    ``insert_audit_event`` OR after an ``IntegrityError`` that proves the event
    was already published by a prior attempt — either way the outbox row is
    done. Published rows are retained for reconciliation (§8 "published 记录
    保留到足以完成对账"); pruning is a separate retention job (PR-045).
    """
    if now is None:
        now = datetime.now(UTC)
    async with sf() as session:
        await session.execute(update(AuditOutboxRow).where(AuditOutboxRow.id == row_id).values(status=OUTBOX_PUBLISHED, published_at=now, updated_at=now, owner_token=None))
        await session.commit()


async def mark_outbox_failed(
    sf: async_sessionmaker[AsyncSession],
    *,
    row_id: str,
    error: object,
    now: datetime | None = None,
) -> str:
    """Record a failed publish: increment attempts, back off, maybe dead-letter.

    Returns the new status (``OUTBOX_PENDING`` if retried, ``OUTBOX_DEAD_LETTER``
    if the threshold was reached). On retry, ``available_at`` is pushed forward
    by the exponential backoff so the row is skipped until then. On dead-letter
    the row stays put for operator inspection (ADR §8 "进入 dead_letter 并 P1/
    P2 告警").
    """
    if now is None:
        now = datetime.now(UTC)
    rendered = _truncate_error(error)
    async with sf() as session:
        row = await session.get(AuditOutboxRow, row_id)
        if row is None:
            await session.rollback()
            raise ValueError(f"audit outbox row {row_id!r} not found")
        new_attempts = row.attempts + 1
        if new_attempts >= DEAD_LETTER_THRESHOLD:
            row.status = OUTBOX_DEAD_LETTER
            row.attempts = new_attempts
            row.last_error = rendered
            row.owner_token = None
            row.updated_at = now
            new_status = OUTBOX_DEAD_LETTER
        else:
            row.status = OUTBOX_PENDING
            row.attempts = new_attempts
            row.last_error = rendered
            row.available_at = now + timedelta(seconds=_backoff_for(new_attempts))
            row.owner_token = None
            row.updated_at = now
            new_status = OUTBOX_PENDING
        await session.commit()
        return new_status


# ---------------------------------------------------------------------------
# Reconciler: release stale ``processing`` (ADR §8 "Reconciler 可以重新释放
# 过期 processing")
# ---------------------------------------------------------------------------


async def release_stale_processing(
    sf: async_sessionmaker[AsyncSession],
    *,
    stale_after_seconds: float = STALE_PROCESSING_SECONDS,
    now: datetime | None = None,
) -> int:
    """Release ``processing`` rows older than the stale window back to ``pending``.

    A worker that crashed mid-publish leaves a row stuck in ``processing``; this
    flips such rows back to ``pending`` (clearing ``owner_token``, resetting
    nothing else — ``attempts`` is bumped by the subsequent failure path, not
    here, so a crash-retry does not consume a retry budget). Returns the count
    released. Run by the worker at the top of each drain cycle.
    """
    if now is None:
        now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=stale_after_seconds)
    async with sf() as session:
        result = await session.execute(
            update(AuditOutboxRow).where(AuditOutboxRow.status == OUTBOX_PROCESSING, AuditOutboxRow.updated_at < cutoff).values(status=OUTBOX_PENDING, owner_token=None, updated_at=now).execution_options(synchronize_session=False)
        )
        await session.commit()
        return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Backlog / observability queries (ADR §8 / §14)
# ---------------------------------------------------------------------------


async def count_pending(sf: async_sessionmaker[AsyncSession], *, now: datetime | None = None) -> int:
    """Count ``pending`` outbox rows (the backlog; ADR §14 ``audit_outbox_pending``)."""
    if now is None:
        now = datetime.now(UTC)
    async with sf() as session:
        stmt = select(func.count()).select_from(AuditOutboxRow).where(AuditOutboxRow.status == OUTBOX_PENDING, AuditOutboxRow.available_at <= now)
        return int((await session.execute(stmt)).scalar_one())


async def count_dead_letter(sf: async_sessionmaker[AsyncSession]) -> int:
    """Count ``dead_letter`` rows (ADR §14 ``audit_dead_letter_total``)."""
    async with sf() as session:
        stmt = select(func.count()).select_from(AuditOutboxRow).where(AuditOutboxRow.status == OUTBOX_DEAD_LETTER)
        return int((await session.execute(stmt)).scalar_one())


async def oldest_pending_age_seconds(
    sf: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
) -> float:
    """Age in seconds of the oldest claimable ``pending`` row (0 if none).

    ADR §14 ``audit_outbox_oldest_age_seconds`` — the SLO alert target
    ("oldest pending >5 分钟：P2").
    """
    if now is None:
        now = datetime.now(UTC)
    async with sf() as session:
        stmt = select(func.min(AuditOutboxRow.available_at)).where(AuditOutboxRow.status == OUTBOX_PENDING, AuditOutboxRow.available_at <= now)
        oldest = (await session.execute(stmt)).scalar_one_or_none()
        if oldest is None:
            return 0.0
        return max(0.0, (now - _as_utc(oldest)).total_seconds())


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_MAX_SECONDS",
    "DEAD_LETTER_THRESHOLD",
    "OUTBOX_DEAD_LETTER",
    "OUTBOX_PENDING",
    "OUTBOX_PROCESSING",
    "OUTBOX_PUBLISHED",
    "STALE_PROCESSING_SECONDS",
    "claim_audit_outbox",
    "count_dead_letter",
    "count_pending",
    "enqueue_audit_outbox",
    "enqueue_audit_outbox_in_session",
    "mark_outbox_failed",
    "mark_outbox_published",
    "oldest_pending_age_seconds",
    "release_stale_processing",
]
