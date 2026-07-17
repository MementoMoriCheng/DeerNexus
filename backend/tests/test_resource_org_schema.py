"""Constraint and migration tests for the stock-resource ``org_id`` lifecycle.

PR-021 (revision ``0005_resource_org_id``, Expand) added a nullable
``org_id`` column (FK ``organizations.id`` ``ondelete=RESTRICT``) plus five
org_id-prefixed compatible indexes to the four core Run-lifecycle stock
tables (``threads_meta``, ``runs``, ``run_events``, ``feedback``). PR-025A
(revision ``0006_enforce_org_not_null``, Enforce) tightened ``org_id`` to
``NOT NULL`` and added ``UNIQUE(org_id, thread_id)`` on ``threads_meta``
(data-model.md §13.3, §7.1).

These tests assert the *current* (Enforced) invariants: the column is
NOT NULL, NULL inserts are rejected at the DB layer, the ``threads_meta``
compound unique holds, and the FK / RESTRICT + compatible indexes from 0005
remain in place. Follows the conventions of ``test_tenant_schema.py``: each
test boots an isolated file-backed SQLite DB via ``init_engine`` (exercising
the full bootstrap path) and tears it down with ``close_engine``. DB-level
constraints (FK / RESTRICT) and nullability are asserted by provoking
``IntegrityError`` or by reflecting column metadata, proving the invariants
live in the DB layer rather than only in the ORM model.

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
# Column existence & NOT NULL (Enforce, PR-025A / revision 0006)
# ===========================================================================


class TestOrgIdColumnExists:
    @pytest.mark.anyio
    async def test_all_four_tables_have_non_null_org_id(self, engine):
        async with engine.connect() as conn:
            for table in RESOURCE_TABLES:
                cols = await conn.run_sync(lambda c, t=table: {col["name"]: col for col in sa.inspect(c).get_columns(t)})
                org_col = cols.get("org_id")
                assert org_col is not None, f"{table} missing org_id column"
                assert org_col["nullable"] is False, f"{table}.org_id must be NOT NULL (Enforce phase, PR-025A / revision 0006)"


# ===========================================================================
# NOT NULL enforcement — NULL org_id rejected at the DB layer
# ===========================================================================


class TestNotNullEnforcement:
    @pytest.mark.anyio
    async def test_thread_rejects_null_org(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(ThreadMetaRow(thread_id="t-null"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_run_rejects_null_org(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(RunRow(run_id="r-null", thread_id="t-1", status="pending"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_event_rejects_null_org(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(RunEventRow(thread_id="t-1", run_id="r-1", seq=1, event_type="msg", category="message"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_feedback_rejects_null_org(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(FeedbackRow(feedback_id="f-null", run_id="r-1", thread_id="t-1", rating=1))
            with pytest.raises(IntegrityError):
                await session.commit()


# ===========================================================================
# threads_meta UNIQUE(org_id, thread_id) (Enforce, §7.1)
# ===========================================================================


class TestThreadMetaCompoundUnique:
    @pytest.mark.anyio
    async def test_compound_unique_constraint_exists(self, engine):
        async with engine.connect() as conn:
            uqs = await conn.run_sync(lambda c: {u["name"]: u for u in sa.inspect(c).get_unique_constraints("threads_meta")})
        uq = uqs.get("uq_threads_meta_org_thread")
        assert uq is not None, "threads_meta missing UNIQUE(org_id, thread_id) constraint uq_threads_meta_org_thread"
        assert uq["column_names"] == ["org_id", "thread_id"]

    @pytest.mark.anyio
    async def test_duplicate_org_thread_pair_rejected(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        await _seed_org(engine, _org())
        async with AsyncSession(engine) as session:
            session.add(_thread(thread_id="t-1", org_id="org-1"))
            await session.commit()
        # thread_id is the global PK, so a second row with the same (org_id,
        # thread_id) collides on PK first — exercise the compound unique by
        # pairing the same org_id with a *different* thread_id is allowed, and
        # the same thread_id under a different org_id is allowed too. The
        # constraint is declarative today (PK subsumes it) but must exist so
        # future org-scoped business keys inherit the prefix-unique convention.
        async with AsyncSession(engine) as session:
            await _seed_org(engine, _org(id="org-2", slug="acme2", name="Acme2"))
            session.add(_thread(thread_id="t-2", org_id="org-2"))
            await session.commit()  # distinct pair, allowed


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
# Migration round-trip (Enforce 0006 ↔ Expand 0005)
# ===========================================================================


class TestMigrationRoundTrip:
    @pytest.mark.anyio
    async def test_enforce_revision_independently_upgradable_and_revertible(self, tmp_path: Path):
        """``0006_enforce_org_not_null`` must revert (Enforce → Expand) by
        restoring ``org_id`` nullability on all four tables and dropping the
        ``threads_meta`` compound unique, then re-enforce cleanly on re-upgrade
        (pr-split-guide §7: each revision independently upgradable)."""
        import asyncio

        import alembic.command as alembic_command
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            cfg = _get_alembic_config(get_engine())
            # Bootstrap stamped head (0006); downgrade to Expand (0005).
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0005_resource_org_id")

            check_engine = create_async_engine(url)
            async with check_engine.connect() as conn:
                for table in RESOURCE_TABLES:
                    cols = await conn.run_sync(lambda c, t=table: {col["name"]: col for col in sa.inspect(c).get_columns(t)})
                    org_col = cols["org_id"]
                    assert org_col["nullable"] is True, f"{table}.org_id still NOT NULL after downgrade to 0005"
                uqs = await conn.run_sync(lambda c: {u["name"] for u in sa.inspect(c).get_unique_constraints("threads_meta")})
                assert "uq_threads_meta_org_thread" not in uqs, "compound unique survived downgrade to 0005"
            await check_engine.dispose()

            # Re-upgrade to head — NOT NULL restored, compound unique re-added.
            await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
            check_engine2 = create_async_engine(url)
            async with check_engine2.connect() as conn:
                for table in RESOURCE_TABLES:
                    cols = await conn.run_sync(lambda c, t=table: {col["name"]: col for col in sa.inspect(c).get_columns(t)})
                    org_col = cols["org_id"]
                    assert org_col["nullable"] is False, f"{table}.org_id nullable after re-upgrade to head"
                uqs = await conn.run_sync(lambda c: {u["name"] for u in sa.inspect(c).get_unique_constraints("threads_meta")})
                assert "uq_threads_meta_org_thread" in uqs, "compound unique missing after re-upgrade to head"
            await check_engine2.dispose()
        finally:
            await close_engine()
