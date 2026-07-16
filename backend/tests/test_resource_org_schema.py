"""Constraint and migration tests for the stock-resource ``org_id`` Expand (PR-021).

Verifies that revision ``0005_resource_org_id`` adds a nullable ``org_id``
column (FK ``organizations.id`` ``ondelete=RESTRICT``) plus five
org_id-prefixed compatible indexes to the four core Run-lifecycle stock
tables (``threads_meta``, ``runs``, ``run_events``, ``feedback``). Follows
the conventions of ``test_tenant_schema.py``: each test boots an isolated
file-backed SQLite DB via ``init_engine`` (exercising the full bootstrap
path) and tears it down with ``close_engine``. DB-level constraints
(FK / RESTRICT) and nullability are asserted by provoking ``IntegrityError``
or by reflecting column metadata, proving the invariants live in the DB
layer rather than only in the ORM model.

Parent (OrganizationRow) rows are committed in a separate session before
child rows are added, mirroring the SQLite FK-at-commit-time constraint
behavior established in ``test_tenant_schema.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.orgs.model import OrganizationRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

RESOURCE_TABLES = ("threads_meta", "runs", "run_events", "feedback")
COMPATIBLE_INDEXES = (
    "ix_threads_meta_org_updated",
    "ix_runs_org_status_created",
    "ix_runs_org_thread_created",
    "ix_events_org_thread_run",
    "ix_feedback_org_thread",
)


def _org(*, id: str = "org-1", slug: str = "acme", name: str = "Acme") -> OrganizationRow:
    return OrganizationRow(id=id, slug=slug, name=name, status="active")


def _thread(*, thread_id: str = "t-1", org_id: str | None = "org-1") -> ThreadMetaRow:
    return ThreadMetaRow(thread_id=thread_id, org_id=org_id)


def _run(*, run_id: str = "r-1", thread_id: str = "t-1", org_id: str | None = "org-1") -> RunRow:
    return RunRow(run_id=run_id, thread_id=thread_id, org_id=org_id, status="pending")


def _event(*, id: int | None = None, thread_id: str = "t-1", run_id: str = "r-1", seq: int = 1, org_id: str | None = "org-1") -> RunEventRow:
    return RunEventRow(id=id, thread_id=thread_id, run_id=run_id, seq=seq, org_id=org_id, event_type="msg", category="message")


def _feedback(*, feedback_id: str = "f-1", run_id: str = "r-1", thread_id: str = "t-1", org_id: str | None = "org-1") -> FeedbackRow:
    return FeedbackRow(feedback_id=feedback_id, run_id=run_id, thread_id=thread_id, org_id=org_id, rating=1)


@pytest.fixture
async def engine(tmp_path: Path):
    """Boot an isolated SQLite DB through the full bootstrap path."""
    from deerflow.persistence.engine import close_engine, get_engine, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'resource_org.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_engine()
    finally:
        await close_engine()


async def _seed_org(engine, org: OrganizationRow) -> None:
    """Commit a parent org in its own session (SQLite FK-at-commit hygiene)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(engine) as session:
        session.add(org)
        await session.commit()


# ===========================================================================
# Column existence & nullability
# ===========================================================================


class TestOrgIdColumnExists:
    @pytest.mark.anyio
    async def test_all_four_tables_have_nullable_org_id(self, engine):
        async with engine.connect() as conn:
            for table in RESOURCE_TABLES:
                cols = await conn.run_sync(lambda c, t=table: {col["name"]: col for col in sa.inspect(c).get_columns(t)})
                org_col = cols.get("org_id")
                assert org_col is not None, f"{table} missing org_id column"
                assert org_col["nullable"] is True, f"{table}.org_id must be nullable (Expand phase)"


# ===========================================================================
# Nullability — legacy rows may stay NULL
# ===========================================================================


class TestNullability:
    @pytest.mark.anyio
    async def test_thread_accepts_null_org(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(ThreadMetaRow(thread_id="t-null"))
            await session.commit()

    @pytest.mark.anyio
    async def test_run_accepts_null_org(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(RunRow(run_id="r-null", thread_id="t-1", status="pending"))
            await session.commit()

    @pytest.mark.anyio
    async def test_event_accepts_null_org(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(RunEventRow(thread_id="t-1", run_id="r-1", seq=1, event_type="msg", category="message"))
            await session.commit()

    @pytest.mark.anyio
    async def test_feedback_accepts_null_org(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(FeedbackRow(feedback_id="f-null", run_id="r-1", thread_id="t-1", rating=1))
            await session.commit()

    @pytest.mark.anyio
    async def test_multiple_null_org_rows_coexist(self, engine):
        # Expand nullability: many legacy rows with NULL org_id must coexist
        # (no UNIQUE / NOT NULL constraint fires on NULL).
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(ThreadMetaRow(thread_id="t-a"))
            session.add(ThreadMetaRow(thread_id="t-b"))
            session.add(ThreadMetaRow(thread_id="t-c"))
            await session.commit()


# ===========================================================================
# FK enforcement — organizations.id, ondelete=RESTRICT
# ===========================================================================


class TestForeignKeyEnforcement:
    @pytest.mark.anyio
    async def test_thread_with_valid_org_succeeds(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        await _seed_org(engine, _org())
        async with AsyncSession(engine) as session:
            session.add(_thread())
            await session.commit()

    @pytest.mark.anyio
    async def test_run_with_valid_org_succeeds(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        await _seed_org(engine, _org())
        async with AsyncSession(engine) as session:
            session.add(_run())
            await session.commit()

    @pytest.mark.anyio
    async def test_event_with_valid_org_succeeds(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        await _seed_org(engine, _org())
        async with AsyncSession(engine) as session:
            session.add(_event())
            await session.commit()

    @pytest.mark.anyio
    async def test_feedback_with_valid_org_succeeds(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        await _seed_org(engine, _org())
        async with AsyncSession(engine) as session:
            session.add(_feedback())
            await session.commit()

    @pytest.mark.anyio
    async def test_thread_with_nonexistent_org_rejected(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_thread(thread_id="t-1", org_id="no-such-org"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_run_with_nonexistent_org_rejected(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_run(run_id="r-1", org_id="no-such-org"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_event_with_nonexistent_org_rejected(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_event(org_id="no-such-org"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_feedback_with_nonexistent_org_rejected(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_feedback(org_id="no-such-org"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_org_delete_restricted_when_referenced(self, engine):
        # ondelete=RESTRICT: hard-deleting an org that a resource references
        # must raise, rather than cascade-deleting tenant resource history.
        from sqlalchemy.ext.asyncio import AsyncSession

        await _seed_org(engine, _org())
        async with AsyncSession(engine) as session:
            session.add(_thread())
            await session.commit()

        async with AsyncSession(engine) as session:
            org = await session.get(OrganizationRow, "org-1")
            await session.delete(org)
            with pytest.raises(IntegrityError):
                await session.commit()


# ===========================================================================
# Compatible indexes
# ===========================================================================


class TestCompatibleIndexes:
    @pytest.mark.anyio
    async def test_all_five_compatible_indexes_created(self, engine):
        async with engine.connect() as conn:
            found: set[str] = set()
            for table in RESOURCE_TABLES:
                idxs = await conn.run_sync(lambda c, t=table: {i["name"] for i in sa.inspect(c).get_indexes(t)})
                found |= idxs
        missing = set(COMPATIBLE_INDEXES) - found
        assert not missing, f"missing compatible indexes: {missing}"


# ===========================================================================
# Migration round-trip (upgrade head ↔ downgrade to 0004)
# ===========================================================================


class TestMigrationRoundTrip:
    @pytest.mark.anyio
    async def test_revision_independently_upgradable_and_revertible(self, tmp_path: Path):
        """``0005_resource_org_id`` must upgrade cleanly on a fresh DB and
        downgrade to remove ``org_id`` from all four tables (pr-split-guide §7:
        each revision independently upgradable)."""
        import asyncio

        import alembic.command as alembic_command
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            cfg = _get_alembic_config(get_engine())
            # Bootstrap already stamped head (0005); downgrade to 0004.
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0004_iam_tables")

            check_engine = create_async_engine(url)
            async with check_engine.connect() as conn:
                for table in RESOURCE_TABLES:
                    cols = await conn.run_sync(lambda c, t=table: {col["name"] for col in sa.inspect(c).get_columns(t)})
                    assert "org_id" not in cols, f"{table}.org_id survived downgrade to 0004"
            await check_engine.dispose()

            # Re-upgrade to head — org_id reappears on all four tables.
            await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
            check_engine2 = create_async_engine(url)
            async with check_engine2.connect() as conn:
                for table in RESOURCE_TABLES:
                    cols = await conn.run_sync(lambda c, t=table: {col["name"] for col in sa.inspect(c).get_columns(t)})
                    assert "org_id" in cols, f"{table}.org_id missing after re-upgrade to head"
            await check_engine2.dispose()
        finally:
            await close_engine()
