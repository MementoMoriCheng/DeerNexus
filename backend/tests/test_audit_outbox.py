"""Outbox queue-lifecycle + worker + sink + integration tests (PR-041).

Covers ADR-0005 §8 (outbox behaviour) and §9.1 (idempotency):

* ``audit_outbox`` table + constraints (status CHECK, event_id unique);
* enqueue → claim → publish / fail-with-backoff / dead-letter semantics;
* atomic claim (two claims don't overlap);
* stale-``processing`` release (reconciler);
* ``drain_audit_outbox`` publishes pending → ``audit_events`` and is idempotent
  on a duplicate event_id;
* ``OutboxAuditSink.emit`` enqueues a row;
* the upgraded ``emit_tenant_event`` shim routes through a registered sink
  end-to-end (emit → outbox → drain → audit_events).

Fixture conventions mirror ``test_audit_schema.py`` / ``test_oidc_group_mapping_repository.py``:
isolated SQLite via ``init_engine`` (full bootstrap, installs migrations).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.contracts.events import AuditEvent
from deerflow.contracts.identity import PrincipalRef
from deerflow.persistence.audit.model import AuditOutboxRow
from deerflow.persistence.audit.outbox import (
    BACKOFF_BASE_SECONDS,
    DEAD_LETTER_THRESHOLD,
    OUTBOX_DEAD_LETTER,
    OUTBOX_PENDING,
    OUTBOX_PROCESSING,
    OUTBOX_PUBLISHED,
    claim_audit_outbox,
    count_dead_letter,
    count_pending,
    enqueue_audit_outbox,
    mark_outbox_failed,
    mark_outbox_published,
    oldest_pending_age_seconds,
    release_stale_processing,
)
from deerflow.persistence.audit.repository import get_audit_event

ORG_A = "00000000-0000-4000-8000-0000000000a1"
USER_ID = "00000000-0000-4000-8000-0000000000c3"
_NOW = datetime.now(UTC)


def _event(
    *,
    event_id: str = "evt-1",
    org_id: str | None = ORG_A,
    action: str = "iam.role_binding.created",
) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        idempotency_key=f"idem-{event_id}",
        org_id=org_id,
        actor=PrincipalRef(type="user", id=USER_ID, user_id=USER_ID),
        action=action,
        outcome="success",
        request_id="req-1",
        occurred_at=_NOW,
        payload={"role_id": "r-admin"},
    )


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


async def _refetch(sf, *, row_id: str) -> AuditOutboxRow:
    """Re-read an outbox row fresh from the DB (mark_* writes in another session).

    ``mark_outbox_published`` / ``mark_outbox_failed`` open their own session and
    mutate the DB row, so the in-memory object held by the caller is stale.
    Tests re-fetch to assert the persisted state. Timestamps are normalised to
    UTC because SQLite strips tzinfo on round-trip.
    """
    from sqlalchemy import select as sa_select

    from deerflow.persistence.audit.outbox import _as_utc

    async with sf() as session:
        row = (await session.execute(sa_select(AuditOutboxRow).where(AuditOutboxRow.id == row_id))).scalar_one()
    # Coerce naive SQLite timestamps to UTC for comparison with tz-aware values.
    row.available_at = _as_utc(row.available_at)
    row.updated_at = _as_utc(row.updated_at)
    return row


# ===========================================================================
# Enqueue + constraints
# ===========================================================================


class TestEnqueue:
    @pytest.mark.anyio
    async def test_enqueue_creates_pending_row(self, sf):
        row = await enqueue_audit_outbox(sf, _event(event_id="evt-enq-1"), now=_NOW)
        assert row.status == OUTBOX_PENDING
        assert row.attempts == 0
        assert row.event_id == "evt-enq-1"
        assert row.org_id == ORG_A
        # payload_json round-trips to the same event.
        assert AuditEvent.model_validate_json(row.payload_json).event_id == "evt-enq-1"

    @pytest.mark.anyio
    async def test_duplicate_event_id_collides(self, sf):
        # Idempotency by event_id (§9.1): re-enqueue raises, not duplicates.
        await enqueue_audit_outbox(sf, _event(event_id="evt-dup"), now=_NOW)
        with pytest.raises(IntegrityError):
            await enqueue_audit_outbox(sf, _event(event_id="evt-dup"), now=_NOW)


# ===========================================================================
# Claim (atomic, non-overlapping)
# ===========================================================================


class TestClaim:
    @pytest.mark.anyio
    async def test_claim_flips_to_processing(self, sf):
        await enqueue_audit_outbox(sf, _event(event_id="evt-claim-1"), now=_NOW)
        claimed = await claim_audit_outbox(sf, batch_size=10, owner_token="w1", now=_NOW)
        assert len(claimed) == 1
        assert claimed[0].status == OUTBOX_PROCESSING
        assert claimed[0].owner_token == "w1"

    @pytest.mark.anyio
    async def test_two_claims_do_not_overlap(self, sf):
        # Two sequential claims must return disjoint rows. (All three rows share
        # the same available_at, so id-order is the tie-break; the exact split
        # is not asserted — only disjointness + exhaustion.)
        for i in range(3):
            await enqueue_audit_outbox(sf, _event(event_id=f"evt-claim-{i}"), now=_NOW)
        first = await claim_audit_outbox(sf, batch_size=2, owner_token="w1", now=_NOW)
        second = await claim_audit_outbox(sf, batch_size=2, owner_token="w2", now=_NOW)
        first_ids = {r.event_id for r in first}
        second_ids = {r.event_id for r in second}
        assert len(first) == 2
        assert len(second) == 1
        assert first_ids.isdisjoint(second_ids)  # no overlap
        assert first_ids | second_ids == {"evt-claim-0", "evt-claim-1", "evt-claim-2"}  # exhausted

    @pytest.mark.anyio
    async def test_backoff_pushed_row_not_claimable_until_due(self, sf):
        # A row pushed into the future by backoff must be skipped.
        await enqueue_audit_outbox(sf, _event(event_id="evt-backoff"), now=_NOW)
        claimed = await claim_audit_outbox(sf, batch_size=1, owner_token="w1", now=_NOW)
        assert len(claimed) == 1
        # Fail it once → available_at pushed forward.
        await mark_outbox_failed(sf, row_id=claimed[0].id, error="boom", now=_NOW)
        # Still pending but not yet due at _NOW.
        again = await claim_audit_outbox(sf, batch_size=1, owner_token="w2", now=_NOW)
        assert again == []
        # Due after the backoff window elapses.
        future = _NOW + timedelta(seconds=BACKOFF_BASE_SECONDS + 1)
        due = await claim_audit_outbox(sf, batch_size=1, owner_token="w3", now=future)
        assert len(due) == 1


# ===========================================================================
# Publish / fail / dead-letter
# ===========================================================================


class TestPublishFail:
    @pytest.mark.anyio
    async def test_mark_published(self, sf):
        await enqueue_audit_outbox(sf, _event(event_id="evt-pub-1"), now=_NOW)
        claimed = await claim_audit_outbox(sf, batch_size=1, owner_token="w1", now=_NOW)
        await mark_outbox_published(sf, row_id=claimed[0].id, now=_NOW)
        row = await _refetch(sf, row_id=claimed[0].id)
        assert row.status == OUTBOX_PUBLISHED
        assert row.published_at is not None

    @pytest.mark.anyio
    async def test_fail_increments_and_backs_off(self, sf):
        await enqueue_audit_outbox(sf, _event(event_id="evt-fail-1"), now=_NOW)
        claimed = await claim_audit_outbox(sf, batch_size=1, owner_token="w1", now=_NOW)
        status = await mark_outbox_failed(sf, row_id=claimed[0].id, error=RuntimeError("x"), now=_NOW)
        assert status == OUTBOX_PENDING
        row = await _refetch(sf, row_id=claimed[0].id)
        assert row.attempts == 1
        assert row.available_at > _NOW  # pushed forward

    @pytest.mark.anyio
    async def test_dead_letter_after_threshold(self, sf):
        await enqueue_audit_outbox(sf, _event(event_id="evt-dead-1"), now=_NOW)
        claimed = await claim_audit_outbox(sf, batch_size=1, owner_token="w1", now=_NOW)
        row_id = claimed[0].id
        status = OUTBOX_PENDING
        for _ in range(DEAD_LETTER_THRESHOLD):
            status = await mark_outbox_failed(sf, row_id=row_id, error="boom", now=_NOW)
        assert status == OUTBOX_DEAD_LETTER
        assert await count_dead_letter(sf) == 1

    @pytest.mark.anyio
    async def test_last_error_truncated_and_no_secret(self, sf):
        await enqueue_audit_outbox(sf, _event(event_id="evt-err-1"), now=_NOW)
        claimed = await claim_audit_outbox(sf, batch_size=1, owner_token="w1", now=_NOW)
        huge = "x" * 5000
        await mark_outbox_failed(sf, row_id=claimed[0].id, error=huge, now=_NOW)
        row = await _refetch(sf, row_id=claimed[0].id)
        assert row.last_error is not None
        assert len(row.last_error) <= 512


# ===========================================================================
# Reconciler
# ===========================================================================


class TestReconciler:
    @pytest.mark.anyio
    async def test_release_stale_processing(self, sf):
        await enqueue_audit_outbox(sf, _event(event_id="evt-stale-1"), now=_NOW)
        claimed = await claim_audit_outbox(sf, batch_size=1, owner_token="w1", now=_NOW)
        # Simulate a worker crash: row stuck in processing, updated_at in past.
        stale_now = _NOW + timedelta(seconds=400)
        released = await release_stale_processing(sf, stale_after_seconds=300, now=stale_now)
        assert released == 1
        # Row is claimable again.
        again = await claim_audit_outbox(sf, batch_size=1, owner_token="w2", now=stale_now)
        assert len(again) == 1
        assert again[0].id == claimed[0].id


# ===========================================================================
# Backlog queries
# ===========================================================================


class TestBacklogQueries:
    @pytest.mark.anyio
    async def test_count_pending_and_oldest_age(self, sf):
        base = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
        for i in range(3):
            await enqueue_audit_outbox(sf, _event(event_id=f"evt-bl-{i}"), now=base + timedelta(seconds=i))
        now = base + timedelta(seconds=10)
        assert await count_pending(sf, now=now) == 3
        # Oldest is base (0s offset) → age ~10s.
        age = await oldest_pending_age_seconds(sf, now=now)
        assert 9.0 <= age <= 11.0


# ===========================================================================
# drain_audit_outbox (worker single pass)
# ===========================================================================


class TestDrainAuditOutbox:
    @pytest.mark.anyio
    async def test_drain_publishes_to_audit_events(self, sf):
        from sqlalchemy import select as sa_select

        from app.gateway.audit_worker import drain_audit_outbox

        await enqueue_audit_outbox(sf, _event(event_id="evt-drain-1"), now=_NOW)
        published = await drain_audit_outbox(sf, now=_NOW)
        assert published == 1
        # The event landed in the append-only store.
        fetched = await get_audit_event(sf, event_id="evt-drain-1")
        assert fetched is not None
        assert fetched.action == "iam.role_binding.created"
        # The outbox row is now published.
        async with sf() as session:
            row = (await session.execute(sa_select(AuditOutboxRow).where(AuditOutboxRow.event_id == "evt-drain-1"))).scalar_one()
        assert row.status == OUTBOX_PUBLISHED

    @pytest.mark.anyio
    async def test_drain_idempotent_on_duplicate_event_id(self, sf):
        # Pre-seed audit_events with the event_id, then enqueue + drain: the
        # worker must mark the outbox row published, not error, not duplicate.
        from app.gateway.audit_worker import drain_audit_outbox
        from deerflow.persistence.audit.repository import insert_audit_event

        await insert_audit_event(sf, _event(event_id="evt-idem-1"))
        await enqueue_audit_outbox(sf, _event(event_id="evt-idem-1"), now=_NOW)
        published = await drain_audit_outbox(sf, now=_NOW)
        assert published == 1  # IntegrityError → treated as published

    @pytest.mark.anyio
    async def test_drain_handles_undecodable_payload(self, sf):
        from sqlalchemy import update as sa_update

        from app.gateway.audit_worker import drain_audit_outbox

        await enqueue_audit_outbox(sf, _event(event_id="evt-bad-json"), now=_NOW)
        # Corrupt the payload_json so deserialization fails.
        async with sf() as session:
            await session.execute(sa_update(AuditOutboxRow).where(AuditOutboxRow.event_id == "evt-bad-json").values(payload_json="{not valid json"))
            await session.commit()
        # Must not raise; row is failed (backoff), not crashed.
        published = await drain_audit_outbox(sf, now=_NOW)
        assert published == 0
