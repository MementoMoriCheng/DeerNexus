"""Tests for the release Idempotency-Key replay-record GC worker (§16.56 follow-up).

Covers :mod:`app.gateway.release_gc_worker`:

* ``sweep_release_idempotency_records`` prunes records older than the retention
  window, leaves fresh records, and is idempotent (a no-op second pass).
* ``run_release_gc_worker`` runs a pass, then stops promptly when ``stop_event``
  is set (mirrors the ``run_audit_worker`` shutdown contract).
* The ``now`` injection point makes retention deterministic.

Fixture conventions mirror ``test_idempotency_repository.py``: boot an isolated
SQLite via ``init_engine``, yield ``get_session_factory()``, tear down with
``close_engine``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from app.gateway.release_gc_worker import (
    GC_RETENTION_DAYS,
    sweep_release_idempotency_records,
)
from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
from deerflow.persistence.release import count_idempotency_records, insert_idempotency_record
from deerflow.persistence.release.model import ReleaseIdempotencyRecordRow

ORG_ID = "org-test"
NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def sf(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'release_gc.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


async def _insert_with_created_at(sf, *, key: str, created_at: datetime, record_id: str) -> None:
    async with sf() as session:
        session.add(
            ReleaseIdempotencyRecordRow(
                id=record_id,
                org_id=ORG_ID,
                idempotency_key=key,
                request_hash="h",
                response_payload={},
                status_code=200,
                created_at=created_at,
            )
        )
        await session.commit()


class TestSweep:
    async def test_prunes_records_older_than_retention(self, sf):
        # Just inside / just outside the default 30-day window.
        stale = NOW - timedelta(days=GC_RETENTION_DAYS + 1)
        fresh = NOW - timedelta(days=GC_RETENTION_DAYS - 1)
        await _insert_with_created_at(sf, key="stale", created_at=stale, record_id="rec-stale")
        await _insert_with_created_at(sf, key="fresh", created_at=fresh, record_id="rec-fresh")

        removed = await sweep_release_idempotency_records(sf, now=NOW)

        assert removed == 1
        assert await count_idempotency_records(sf) == 1
        async with sf() as session:
            from deerflow.persistence.release import get_idempotency_record

            assert await get_idempotency_record(session, org_id=ORG_ID, idempotency_key="fresh") is not None
            assert await get_idempotency_record(session, org_id=ORG_ID, idempotency_key="stale") is None

    async def test_boundary_record_at_exactly_cutoff_survives(self, sf):
        """A record exactly at the cutoff (now - retention) is NOT pruned (strict <)."""
        boundary = NOW - timedelta(days=GC_RETENTION_DAYS)
        await _insert_with_created_at(sf, key="edge", created_at=boundary, record_id="rec-edge")

        removed = await sweep_release_idempotency_records(sf, now=NOW)

        assert removed == 0
        assert await count_idempotency_records(sf) == 1

    async def test_sweep_is_idempotent_second_pass_noop(self, sf):
        stale = NOW - timedelta(days=GC_RETENTION_DAYS + 5)
        await _insert_with_created_at(sf, key="stale", created_at=stale, record_id="rec-stale")
        await sweep_release_idempotency_records(sf, now=NOW)

        second = await sweep_release_idempotency_records(sf, now=NOW)

        assert second == 0

    async def test_custom_retention_days(self, sf):
        created = NOW - timedelta(days=2)
        await _insert_with_created_at(sf, key="two-day-old", created_at=created, record_id="rec-2d")

        # 1-day retention prunes the 2-day-old record.
        removed = await sweep_release_idempotency_records(sf, retention_days=1, now=NOW)
        assert removed == 1

    async def test_empty_table_sweep_returns_zero(self, sf):
        assert await sweep_release_idempotency_records(sf, now=NOW) == 0

    async def test_uses_real_now_when_omitted(self, sf):
        """Without `now`, the cutoff is now() - retention. A back-dated record is pruned."""
        async with sf() as session:
            await insert_idempotency_record(
                session,
                org_id=ORG_ID,
                idempotency_key="legacy",
                request_hash="h",
                response_payload={},
                status_code=200,
                record_id="rec-legacy",
            )
            await session.commit()
        # Back-date it well past the window.
        async with sf() as session:
            row = await session.get(ReleaseIdempotencyRecordRow, "rec-legacy")
            assert row is not None
            row.created_at = datetime.now(UTC) - timedelta(days=GC_RETENTION_DAYS + 10)
            await session.commit()

        removed = await sweep_release_idempotency_records(sf)  # now=None -> datetime.now(UTC)
        assert removed == 1


class TestWorkerLoop:
    async def test_runs_a_pass_then_stops_on_event(self, sf):
        """The worker performs at least one sweep, then exits promptly when stop is set."""
        from app.gateway.release_gc_worker import run_release_gc_worker

        # Back-date a record so the first sweep prunes it.
        stale = datetime.now(UTC) - timedelta(days=GC_RETENTION_DAYS + 1)
        await _insert_with_created_at(sf, key="stale", created_at=stale, record_id="rec-stale")
        assert await count_idempotency_records(sf) == 1

        stop = asyncio.Event()
        # Tiny interval so the first pass happens almost immediately.
        task = asyncio.create_task(
            run_release_gc_worker(sf, interval=0.05, stop_event=stop),
        )
        # Give the worker time for one sweep pass.
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)

        # The stale record was pruned by the sweep.
        assert await count_idempotency_records(sf) == 0

    async def test_sweep_exception_does_not_kill_worker(self, sf):
        """A failing sweep pass must not terminate the loop (resilience contract)."""
        from app.gateway.release_gc_worker import run_release_gc_worker

        call_count = 0

        class _BrokenSF:
            async def __call__(self):
                # Mimic session factory callability.
                raise RuntimeError("boom")

        # Patch the module's sweep to raise on first call, succeed after.
        import app.gateway.release_gc_worker as mod

        original = mod.sweep_release_idempotency_records

        async def flaky(_sf, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated sweep failure")
            return 0

        mod.sweep_release_idempotency_records = flaky
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_release_gc_worker(sf, interval=0.02, stop_event=stop),
        )
        try:
            await asyncio.sleep(0.15)
            # Worker survived the first (failing) pass and is still running.
            assert not task.done()
            assert call_count >= 1
        finally:
            mod.sweep_release_idempotency_records = original
            stop.set()
            await asyncio.wait_for(task, timeout=5.0)
