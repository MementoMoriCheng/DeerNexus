"""Constraint and migration tests for the tenant control-plane tables (PR-020A).

Verifies that the four tables introduced by revision ``0003_tenant_tables``
(``organizations``, ``workspaces``, ``external_identities``,
``org_memberships``) exist after bootstrap, enforce their declared
constraints (CHECK / UNIQUE / FK / partial unique index), and round-trip
through ``alembic upgrade`` / ``downgrade``.

Follows the conventions of ``test_channel_connections_repository.py`` and
``test_persistence_bootstrap.py``: each test boots an isolated file-backed
SQLite DB via ``init_engine`` (exercising the full bootstrap path) and
tears it down with ``close_engine``. DB-level constraints are asserted by
provoking ``IntegrityError`` with a manual insert, proving the invariant
lives in the DB layer rather than only in the ORM model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.orgs.model import (
    ExternalIdentityRow,
    OrganizationRow,
    OrgMembershipRow,
    WorkspaceRow,
)
from deerflow.persistence.user.model import UserRow

TENANT_TABLES = {"organizations", "workspaces", "external_identities", "org_memberships"}
DELETED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _org(
    *,
    id: str = "org-1",
    slug: str = "acme",
    name: str = "Acme",
    status: str = "active",
    deleted_at: datetime | None = None,
) -> OrganizationRow:
    return OrganizationRow(id=id, slug=slug, name=name, status=status, deleted_at=deleted_at)


@pytest.fixture
async def engine(tmp_path: Path):
    """Boot an isolated SQLite DB through the full bootstrap path."""
    from deerflow.persistence.engine import close_engine, get_engine, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'tenant.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_engine()
    finally:
        await close_engine()


# ===========================================================================
# Table existence
# ===========================================================================


class TestTenantTablesExist:
    @pytest.mark.anyio
    async def test_all_four_tables_created_by_bootstrap(self, engine):
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
        assert TENANT_TABLES <= names, f"missing tenant tables: {TENANT_TABLES - names}"


# ===========================================================================
# organizations constraints
# ===========================================================================


class TestOrganizationConstraints:
    @pytest.mark.anyio
    async def test_partial_unique_slug_allows_reuse_after_soft_delete(self, engine):
        # A soft-deleted org releases its slug; a new org may claim it.
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_org(id="org-old", slug="acme", name="Old", status="deleted", deleted_at=DELETED_AT))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(_org(id="org-new", slug="acme", name="New", status="active"))
            await session.commit()  # must NOT raise — slug freed by deleted_at

    @pytest.mark.anyio
    async def test_partial_unique_slug_rejects_duplicate_active(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_org(id="org-a", slug="acme"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(_org(id="org-b", slug="acme", name="Acme B"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_status_check_rejects_invalid_value(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_org(id="org-x", slug="x", name="X", status="bogus"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_row_version_defaults_to_one(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            org = _org(id="org-1", slug="s")
            session.add(org)
            await session.commit()
            await session.refresh(org)
            assert org.row_version == 1


# ===========================================================================
# workspaces constraints
# ===========================================================================


class TestWorkspaceConstraints:
    @pytest.mark.anyio
    async def test_unique_org_slug(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_org(id="org-1", slug="o"))
            session.add(WorkspaceRow(id="ws-1", org_id="org-1", slug="dev", name="Dev", status="active"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(WorkspaceRow(id="ws-2", org_id="org-1", slug="dev", name="Dup", status="active"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_status_check_rejects_invalid_value(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_org(id="org-1", slug="o"))
            session.add(WorkspaceRow(id="ws-1", org_id="org-1", slug="dev", name="Dev", status="bogus"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_fk_org_cascade_on_delete(self, engine):
        # Deleting an org should cascade-delete its workspaces.
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_org(id="org-1", slug="o"))
            session.add(WorkspaceRow(id="ws-1", org_id="org-1", slug="dev", name="Dev", status="active"))
            await session.commit()

        async with AsyncSession(engine) as session:
            org = await session.get(OrganizationRow, "org-1")
            await session.delete(org)
            await session.commit()

        async with AsyncSession(engine) as session:
            assert await session.get(WorkspaceRow, "ws-1") is None


# ===========================================================================
# external_identities constraints
# ===========================================================================


class TestExternalIdentityConstraints:
    @pytest.mark.anyio
    async def test_unique_issuer_subject(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(UserRow(id="u-1", email="a@x.com", system_role="user"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(ExternalIdentityRow(id="ei-1", user_id="u-1", issuer="https://idp", subject="sub-1", provider="oidc"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(ExternalIdentityRow(id="ei-2", user_id="u-1", issuer="https://idp", subject="sub-1", provider="oidc"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_fk_user_required(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(ExternalIdentityRow(id="ei-1", user_id="nonexistent", issuer="https://idp", subject="sub-1", provider="oidc"))
            with pytest.raises(IntegrityError):
                await session.commit()


# ===========================================================================
# org_memberships constraints
# ===========================================================================


class TestOrgMembershipConstraints:
    @pytest.mark.anyio
    async def test_unique_org_user(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_org(id="org-1", slug="o"))
            session.add(UserRow(id="u-1", email="a@x.com", system_role="user"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(OrgMembershipRow(id="m-1", org_id="org-1", user_id="u-1", status="active"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(OrgMembershipRow(id="m-2", org_id="org-1", user_id="u-1", status="suspended"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_status_check_rejects_invalid_value(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_org(id="org-1", slug="o"))
            session.add(UserRow(id="u-1", email="a@x.com", system_role="user"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(OrgMembershipRow(id="m-1", org_id="org-1", user_id="u-1", status="bogus"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_user_status_index_exists(self, engine):
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: {idx["name"] for idx in sa.inspect(c).get_indexes("org_memberships")})
        assert "idx_org_memberships_user_status" in indexes


# ===========================================================================
# Migration round-trip (upgrade head ↔ downgrade to 0002)
# ===========================================================================


class TestMigrationRoundTrip:
    @pytest.mark.anyio
    async def test_revision_independently_upgradable_and_revertible(self, tmp_path: Path):
        """``0003_tenant_tables`` must upgrade cleanly on a fresh DB and
        downgrade to remove all four tables (pr-split-guide §7: each revision
        independently upgradable)."""
        import asyncio

        import alembic.command as alembic_command
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            cfg = _get_alembic_config(get_engine())
            # Bootstrap already stamped head (0003); downgrade to 0002.
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0002_runs_token_usage")

            check_engine = create_async_engine(url)
            async with check_engine.connect() as conn:
                names = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
            await check_engine.dispose()

            assert TENANT_TABLES.isdisjoint(names), "tenant tables survived downgrade to 0002"

            # Re-upgrade to head — tables reappear.
            await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
            check_engine2 = create_async_engine(url)
            async with check_engine2.connect() as conn:
                names2 = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
            await check_engine2.dispose()
            assert TENANT_TABLES <= names2, "tenant tables missing after re-upgrade to head"
        finally:
            await close_engine()
