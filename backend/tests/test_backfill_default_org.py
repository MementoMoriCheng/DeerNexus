"""Tests for the default-Org backfill job (PR-023).

Verifies that ``backfill_resource_org`` fills legacy NULL ``org_id`` rows on
the four resource tables (threads_meta / runs / run_events / feedback) with
the default Org, in dependency order, idempotently, with batched/throttled
commits, a non-mutating dry-run, and the post-backfill validation gates
(row-count invariance, no-null-org, FK integrity).

Follows the conventions of ``test_default_org_bootstrap.py`` /
``test_resource_org_schema.py``: each test boots an isolated file-backed
SQLite DB via ``init_engine`` and tears it down with ``close_engine``.
Parent rows are committed in their own session before children (SQLite
FK-at-commit hygiene).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.orgs.model import OrganizationRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.tenancy import backfill_resource_org, ensure_default_org

DEFAULT_ORG_ID = "default"
ALT_ORG_ID = "org-other"


@pytest.fixture
async def sf(tmp_path: Path):
    """Boot an isolated SQLite DB pinned to the pre-Enforce (0005) schema.

    The backfill job fills legacy NULL ``org_id`` rows — that only makes sense
    against a schema where ``org_id`` is still nullable, i.e. the Expand-phase
    shape (revision ``0005_resource_org_id``). PR-025A Enforce (revision
    ``0006_enforce_org_not_null``) makes the column NOT NULL, so a head-schema
    DB refuses the NULL-org seed rows this suite relies on. We therefore
    bootstrap to head (NOT NULL) and then downgrade to 0005 (nullable) before
    seeding. This pins the DB to the exact schema the backfill job targets,
    and as a side effect exercises the 0006 downgrade round-trip.

    Post-Enforce, no freshly-provisioned DB can hold NULL ``org_id`` (``create_all``
    builds NOT NULL, head migration enforces it); the backfill job remains useful
    only for the deployment-transition case of a DB provisioned before 0006 ran.
    """
    from alembic import command as alembic_command

    from deerflow.persistence.bootstrap import _get_alembic_config
    from deerflow.persistence.engine import close_engine, get_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'backfill.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    # Roll the freshly-bootstrapped (head) schema back to the pre-Enforce
    # shape so NULL org_id rows can be seeded. init_engine has already stamped
    # alembic_version to head, so a plain downgrade lands at 0005 cleanly.
    # ``get_engine()`` reads the module-global live (post-init_engine) instead
    # of a snapshot taken at import time.
    await asyncio.to_thread(alembic_command.downgrade, _get_alembic_config(get_engine()), "0005_resource_org_id")
    try:
        yield get_session_factory()
    finally:
        await close_engine()


async def _seed_org(sf, *, org_id: str = DEFAULT_ORG_ID, slug: str = "default", name: str = "Default") -> None:
    async with sf() as session:
        session.add(OrganizationRow(id=org_id, slug=slug, name=name, status="active"))
        await session.commit()


async def _count_null_org(sf, model: type) -> int:
    async with sf() as session:
        return await session.scalar(select(sa.func.count()).select_from(model).where(model.org_id.is_(None))) or 0


async def _count_total(sf, model: type) -> int:
    async with sf() as session:
        return await session.scalar(select(sa.func.count()).select_from(model)) or 0


async def _seed_thread(sf, *, thread_id: str, org_id: str | None) -> None:
    async with sf() as session:
        session.add(ThreadMetaRow(thread_id=thread_id, org_id=org_id))
        await session.commit()


async def _seed_run(sf, *, run_id: str, thread_id: str, org_id: str | None) -> None:
    async with sf() as session:
        session.add(RunRow(run_id=run_id, thread_id=thread_id, org_id=org_id, status="pending"))
        await session.commit()


async def _seed_event(sf, *, run_id: str, thread_id: str, seq: int, org_id: str | None) -> None:
    async with sf() as session:
        session.add(RunEventRow(thread_id=thread_id, run_id=run_id, seq=seq, org_id=org_id, event_type="msg", category="message"))
        await session.commit()


async def _seed_feedback(sf, *, feedback_id: str, run_id: str, thread_id: str, org_id: str | None) -> None:
    async with sf() as session:
        session.add(FeedbackRow(feedback_id=feedback_id, run_id=run_id, thread_id=thread_id, org_id=org_id, rating=1))
        await session.commit()


async def _seed_legacy_rows(sf) -> dict[type, int]:
    """Seed one NULL-org and one already-assigned row per table; return totals."""
    # Orgs must exist for the assigned rows' FK (ALT_ORG_ID) and the default.
    await _seed_org(sf, org_id=DEFAULT_ORG_ID)
    await _seed_org(sf, org_id=ALT_ORG_ID, slug="other", name="Other")

    await _seed_thread(sf, thread_id="t-null", org_id=None)
    await _seed_thread(sf, thread_id="t-set", org_id=ALT_ORG_ID)
    await _seed_run(sf, run_id="r-null", thread_id="t-null", org_id=None)
    await _seed_run(sf, run_id="r-set", thread_id="t-set", org_id=ALT_ORG_ID)
    await _seed_event(sf, run_id="r-null", thread_id="t-null", seq=1, org_id=None)
    await _seed_event(sf, run_id="r-set", thread_id="t-set", seq=1, org_id=ALT_ORG_ID)
    await _seed_feedback(sf, feedback_id="f-null", run_id="r-null", thread_id="t-null", org_id=None)
    await _seed_feedback(sf, feedback_id="f-set", run_id="r-set", thread_id="t-set", org_id=ALT_ORG_ID)

    return {
        ThreadMetaRow: 2,
        RunRow: 2,
        RunEventRow: 2,
        FeedbackRow: 2,
    }


# ===========================================================================
# Dry-run
# ===========================================================================


class TestDryRun:
    @pytest.mark.anyio
    async def test_dry_run_counts_candidates_and_does_not_mutate(self, sf):
        await _seed_legacy_rows(sf)
        before_nulls = {m: await _count_null_org(sf, m) for m in (ThreadMetaRow, RunRow, RunEventRow, FeedbackRow)}

        report = await backfill_resource_org(sf, org_id=DEFAULT_ORG_ID, dry_run=True)

        assert report.dry_run is True
        assert report.total_updated == 0
        assert len(report.tables) == 4
        for result in report.tables:
            assert result.updated_rows == 0
            assert result.batches == 0
            assert result.after_null_count == result.before_null_count

        # DB unchanged: NULL counts identical.
        for m in (ThreadMetaRow, RunRow, RunEventRow, FeedbackRow):
            assert await _count_null_org(sf, m) == before_nulls[m]


# ===========================================================================
# Backfill fills NULL rows, leaves assigned rows untouched
# ===========================================================================


class TestBackfillFillsNullRows:
    @pytest.mark.anyio
    async def test_null_rows_assigned_default_assigned_rows_unchanged(self, sf):
        await _seed_legacy_rows(sf)

        await backfill_resource_org(sf, org_id=DEFAULT_ORG_ID, batch_size=500, throttle_ms=0)

        async with sf() as session:
            null_thread = await session.get(ThreadMetaRow, "t-null")
            set_thread = await session.get(ThreadMetaRow, "t-set")
            null_run = await session.get(RunRow, "r-null")
            set_run = await session.get(RunRow, "r-set")
            null_event = (await session.execute(select(RunEventRow).where(RunEventRow.run_id == "r-null", RunEventRow.seq == 1))).scalar_one()
            null_feedback = await session.get(FeedbackRow, "f-null")
            set_feedback = await session.get(FeedbackRow, "f-set")

        assert null_thread.org_id == DEFAULT_ORG_ID
        assert set_thread.org_id == ALT_ORG_ID  # untouched
        assert null_run.org_id == DEFAULT_ORG_ID
        assert set_run.org_id == ALT_ORG_ID
        assert null_event.org_id == DEFAULT_ORG_ID
        assert null_feedback.org_id == DEFAULT_ORG_ID
        assert set_feedback.org_id == ALT_ORG_ID


# ===========================================================================
# Idempotent re-run
# ===========================================================================


class TestIdempotentRerun:
    @pytest.mark.anyio
    async def test_second_run_updates_zero(self, sf):
        await _seed_legacy_rows(sf)

        first = await backfill_resource_org(sf, org_id=DEFAULT_ORG_ID, throttle_ms=0)
        second = await backfill_resource_org(sf, org_id=DEFAULT_ORG_ID, throttle_ms=0)

        assert first.total_updated == 4  # one NULL row per table
        assert second.total_updated == 0  # candidate set empty
        for result in second.tables:
            assert result.before_null_count == 0
            assert result.updated_rows == 0


# ===========================================================================
# Validation gates
# ===========================================================================


class TestValidation:
    @pytest.mark.anyio
    async def test_row_count_invariant(self, sf):
        totals = await _seed_legacy_rows(sf)

        await backfill_resource_org(sf, org_id=DEFAULT_ORG_ID, throttle_ms=0)

        for model, expected in totals.items():
            assert await _count_total(sf, model) == expected  # only org_id changed

    @pytest.mark.anyio
    async def test_no_null_org_after_backfill(self, sf):
        await _seed_legacy_rows(sf)
        report = await backfill_resource_org(sf, org_id=DEFAULT_ORG_ID, throttle_ms=0)

        assert report.passed is True
        for model in (ThreadMetaRow, RunRow, RunEventRow, FeedbackRow):
            assert await _count_null_org(sf, model) == 0
            assert report.validation["no_null_org"][model.__tablename__] == 0

    @pytest.mark.anyio
    async def test_fk_integrity_no_orphans(self, sf):
        await _seed_legacy_rows(sf)
        report = await backfill_resource_org(sf, org_id=DEFAULT_ORG_ID, throttle_ms=0)

        assert report.validation["orphan_fk"]  # gate present
        for count in report.validation["orphan_fk"].values():
            assert count == 0


# ===========================================================================
# Dependency order
# ===========================================================================


class TestDependencyOrder:
    @pytest.mark.anyio
    async def test_tables_processed_in_dependency_order(self, sf):
        await _seed_legacy_rows(sf)
        report = await backfill_resource_org(sf, org_id=DEFAULT_ORG_ID, throttle_ms=0)

        ordered_names = [t.table for t in report.tables]
        assert ordered_names == ["threads_meta", "runs", "run_events", "feedback"]


# ===========================================================================
# Batch + throttle
# ===========================================================================


class TestBatchThrottle:
    @pytest.mark.anyio
    async def test_small_batch_multiple_passes(self, sf):
        await _seed_org(sf)
        # 5 NULL-org threads with batch_size=2 -> 3 batches (2 + 2 + 1).
        for i in range(5):
            await _seed_thread(sf, thread_id=f"t-{i}", org_id=None)

        report = await backfill_resource_org(sf, org_id=DEFAULT_ORG_ID, batch_size=2, throttle_ms=0)

        thread_result = next(t for t in report.tables if t.table == "threads_meta")
        assert thread_result.before_null_count == 5
        assert thread_result.updated_rows == 5
        assert thread_result.batches == 3
        assert thread_result.after_null_count == 0


# ===========================================================================
# Default Org precondition (ensure_default_org before backfill)
# ===========================================================================


class TestEnsureDefaultOrgPrecondition:
    @pytest.mark.anyio
    async def test_backfill_succeeds_after_ensure_default_org(self, sf):
        # No org exists yet; the caller ensures it first (as the CLI does).
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug="default", name="Default")
        await _seed_thread(sf, thread_id="t-1", org_id=None)

        report = await backfill_resource_org(sf, org_id=DEFAULT_ORG_ID, throttle_ms=0)

        assert report.total_updated == 1
        assert report.passed is True
