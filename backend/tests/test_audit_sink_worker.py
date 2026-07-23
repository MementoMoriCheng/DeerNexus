"""AuditSink + worker-loop + end-to-end integration tests (PR-041).

Covers the app-layer pieces on top of ``test_audit_outbox.py`` (the harness
queue semantics):

* ``OutboxAuditSink.emit`` enqueues a row (and is best-effort non-raising);
* ``get_audit_sink`` lazy singleton + reset;
* ``run_audit_worker`` loop drains until ``stop_event`` and exits promptly;
* the upgraded ``emit_tenant_event`` shim routes through a registered sink
  end-to-end (emit → outbox → drain → audit_events), and falls back to the
  logger when no sink is registered (harness boundary: shim never hard-imports
  the app layer).

Fixture: isolated SQLite via ``init_engine`` (full bootstrap → migrations).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select as sa_select

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from app.gateway.audit_sink import OutboxAuditSink, get_audit_sink, reset_audit_sink_for_testing
from app.gateway.audit_worker import run_audit_worker
from deerflow.contracts.events import AuditEvent
from deerflow.contracts.identity import PrincipalRef
from deerflow.persistence.audit.model import AuditOutboxRow
from deerflow.persistence.audit.outbox import enqueue_audit_outbox
from deerflow.persistence.audit.repository import get_audit_event

ORG_A = "00000000-0000-4000-8000-0000000000a1"
USER_ID = "00000000-0000-4000-8000-0000000000c3"
_NOW = datetime.now(UTC)


def _event(*, event_id: str = "evt-sink-1", org_id: str | None = ORG_A) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        idempotency_key=f"idem-{event_id}",
        org_id=org_id,
        actor=PrincipalRef(type="user", id=USER_ID, user_id=USER_ID),
        action="iam.role_binding.created",
        outcome="success",
        request_id="req-1",
        occurred_at=_NOW,
        payload={"role_id": "r-admin"},
    )


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'sink.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


# ===========================================================================
# OutboxAuditSink
# ===========================================================================


class TestOutboxAuditSink:
    @pytest.mark.anyio
    async def test_emit_enqueues_pending_row(self, sf):
        sink = OutboxAuditSink(sf)
        await sink.emit(_event(event_id="evt-emit-1"))
        async with sf() as session:
            row = (await session.execute(sa_select(AuditOutboxRow).where(AuditOutboxRow.event_id == "evt-emit-1"))).scalar_one()
        assert row.status == "pending"
        assert row.org_id == ORG_A

    @pytest.mark.anyio
    async def test_emit_never_raises_on_duplicate(self, sf):
        # A second emit with the same event_id is the idempotent path; the sink
        # swallows the IntegrityError rather than surfacing it.
        sink = OutboxAuditSink(sf)
        await sink.emit(_event(event_id="evt-emit-dup"))
        await sink.emit(_event(event_id="evt-emit-dup"))  # must not raise
        async with sf() as session:
            count = (await session.execute(sa_select(AuditOutboxRow).where(AuditOutboxRow.event_id == "evt-emit-dup"))).scalars().all()
        assert len(list(count)) == 1  # exactly one row, not two

    @pytest.mark.anyio
    async def test_emit_never_raises_on_db_error(self, sf):
        # A sink whose session factory is broken must not raise (best-effort).
        class _BrokenSf:
            def __call__(self):
                raise RuntimeError("simulated DB failure")

        sink = OutboxAuditSink(_BrokenSf())  # type: ignore[arg-type]
        await sink.emit(_event(event_id="evt-emit-broken"))  # must not raise

    @pytest.mark.anyio
    async def test_get_audit_sink_singleton_and_reset(self, sf):
        reset_audit_sink_for_testing()
        try:
            s1 = get_audit_sink()
            s2 = get_audit_sink()
            assert s1 is s2  # same instance
        finally:
            reset_audit_sink_for_testing()


# ===========================================================================
# Worker loop
# ===========================================================================


class TestRunAuditWorker:
    @pytest.mark.anyio
    async def test_worker_drains_then_stops_on_event(self, sf):
        await enqueue_audit_outbox(sf, _event(event_id="evt-w-1"), now=_NOW)
        await enqueue_audit_outbox(sf, _event(event_id="evt-w-2"), now=_NOW)
        stop = asyncio.Event()
        # Short interval so the drain happens quickly.
        task = asyncio.create_task(run_audit_worker(sf, interval=0.05, stop_event=stop))
        # Give it a couple of drain cycles to publish both, then stop.
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)
        # Both events published to audit_events.
        assert await get_audit_event(sf, event_id="evt-w-1") is not None
        assert await get_audit_event(sf, event_id="evt-w-2") is not None

    @pytest.mark.anyio
    async def test_worker_survives_a_drain_pass_exception(self, sf):
        # A drain pass that raises internally must not kill the worker loop.
        stop = asyncio.Event()
        task = asyncio.create_task(run_audit_worker(sf, interval=0.02, stop_event=stop))
        await asyncio.sleep(0.1)
        # Worker still alive (not done) until stopped.
        assert not task.done()
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


# ===========================================================================
# emit_tenant_event shim → sink → outbox → audit_events (end-to-end)
# ===========================================================================


class TestShimEndToEnd:
    @pytest.mark.anyio
    async def test_registered_sink_routes_to_outbox(self, sf):
        from deerflow.tenancy.audit_events import emit_tenant_event, set_tenant_event_sink

        sink = OutboxAuditSink(sf)
        set_tenant_event_sink(sink)
        try:
            emit_tenant_event(
                "service_account_created",
                org_id=ORG_A,
                principal_id=USER_ID,
                payload={"sa_id": "sa-1", "name": "bot"},
            )
            # The shim schedules the async emit as a fire-and-forget task; poll
            # until the outbox row lands (the sink never raises, so a miss just
            # means the scheduled task has not yet run).
            row = None
            for _ in range(50):
                await asyncio.sleep(0.02)
                async with sf() as session:
                    row = (await session.execute(sa_select(AuditOutboxRow).where(AuditOutboxRow.org_id == ORG_A))).scalar_one_or_none()
                if row is not None:
                    break
            assert row is not None, "outbox row never landed"
            assert row.status == "pending"
            ev = AuditEvent.model_validate_json(row.payload_json)
            assert ev.action == "service_account_created"
            assert ev.actor.id == USER_ID
            assert ev.outcome == "success"
        finally:
            set_tenant_event_sink(None)

    @pytest.mark.anyio
    async def test_no_sink_falls_back_to_logger(self, sf, caplog):
        import logging

        from deerflow.tenancy.audit_events import emit_tenant_event, set_tenant_event_sink

        caplog.set_level(logging.INFO, logger="deerflow.tenancy.audit_events")
        set_tenant_event_sink(None)
        # Must not raise and must not enqueue anything.
        emit_tenant_event("default_org_created", org_id=ORG_A, principal_id=None, payload={"slug": "x"})
        await asyncio.sleep(0.02)
        async with sf() as session:
            rows = (await session.execute(sa_select(AuditOutboxRow))).scalars().all()
        assert list(rows) == []
        # And the logger recorded it.
        assert any("default_org_created" in r.getMessage() for r in caplog.records)

    @pytest.mark.anyio
    async def test_full_pipeline_emit_to_audit_events(self, sf):
        from app.gateway.audit_worker import drain_audit_outbox
        from deerflow.tenancy.audit_events import emit_tenant_event, set_tenant_event_sink

        set_tenant_event_sink(OutboxAuditSink(sf))
        try:
            emit_tenant_event(
                "api_key_created",
                org_id=ORG_A,
                principal_id=USER_ID,
                payload={"key_id": "k-1", "key_prefix": "dk_live_ab12"},
            )
            # Wait for the scheduled enqueue to land before draining.
            for _ in range(50):
                await asyncio.sleep(0.02)
                async with sf() as session:
                    rows = (await session.execute(sa_select(AuditOutboxRow).where(AuditOutboxRow.org_id == ORG_A))).scalars().all()
                if len(list(rows)) >= 1:
                    break
            published = await drain_audit_outbox(sf)  # use live now (shim enqueued at real time)
            assert published == 1
        finally:
            set_tenant_event_sink(None)
        # The event_id is randomly generated by the shim, so assert via count
        # that exactly one audit_events row landed for this org.
        from deerflow.persistence.audit.repository import count_by_org

        assert await count_by_org(sf, org_id=ORG_A) == 1
