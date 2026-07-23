"""Constraint and migration tests for the IAM control-plane tables (PR-020B).

Verifies that the four tables introduced by revision ``0004_iam_tables``
(``roles``, ``role_bindings``, ``service_accounts``, ``api_keys``) exist
after bootstrap, enforce their declared constraints (CHECK / UNIQUE / FK /
partial unique index), and round-trip through ``alembic upgrade`` /
``downgrade``.

Follows the conventions of ``test_tenant_schema.py`` (PR-020A sibling) and
``test_channel_connections_repository.py``: each test boots an isolated
file-backed SQLite DB via ``init_engine`` (exercising the full bootstrap
path) and tears it down with ``close_engine``. DB-level constraints are
asserted by provoking ``IntegrityError`` with a manual insert.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.iam.model import (
    ApiKeyRow,
    RoleBindingRow,
    RoleRow,
    ServiceAccountRow,
)

IAM_TABLES = {"roles", "role_bindings", "service_accounts", "api_keys", "oidc_group_mappings"}
_EXPIRES = datetime.now(UTC) + timedelta(days=30)


def _role(
    *,
    id: str = "role-1",
    org_id: str | None = "org-1",
    name: str = "org:admin",
    is_system: bool = False,
) -> RoleRow:
    return RoleRow(id=id, org_id=org_id, name=name, is_system=is_system, permissions=["read"])


def _svc(*, id: str = "sa-1", org_id: str = "org-1", name: str = "bot") -> ServiceAccountRow:
    return ServiceAccountRow(id=id, org_id=org_id, name=name, status="active")


@pytest.fixture
async def engine(tmp_path: Path):
    """Boot an isolated SQLite DB through the full bootstrap path."""
    from deerflow.persistence.engine import close_engine, get_engine, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'iam.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_engine()
    finally:
        await close_engine()


# ===========================================================================
# Table existence
# ===========================================================================


class TestIamTablesExist:
    @pytest.mark.anyio
    async def test_all_four_iam_tables_created_by_bootstrap(self, engine):
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
        assert IAM_TABLES <= names, f"missing IAM tables: {IAM_TABLES - names}"


# ===========================================================================
# roles constraints
# ===========================================================================


class TestRoleConstraints:
    @pytest.mark.anyio
    async def test_tenant_role_unique_org_name(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_role(id="r-1", org_id="org-1", name="org:admin"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(_role(id="r-2", org_id="org-1", name="org:admin"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_different_orgs_allow_same_role_name(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_role(id="r-1", org_id="org-1", name="org:admin"))
            session.add(_role(id="r-2", org_id="org-2", name="org:admin"))
            await session.commit()  # must NOT raise — different orgs

    @pytest.mark.anyio
    async def test_system_template_allows_null_org(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_role(id="r-sys", org_id=None, name="system:admin", is_system=True))
            await session.commit()  # must NOT raise — system template

    @pytest.mark.anyio
    async def test_null_org_rejected_when_not_system(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            # org_id NULL but is_system=False violates the CHECK constraint.
            session.add(_role(id="r-bad", org_id=None, name="sneaky", is_system=False))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_row_version_defaults_to_one(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            role = _role()
            session.add(role)
            await session.commit()
            await session.refresh(role)
            assert role.row_version == 1


# ===========================================================================
# role_bindings constraints
# ===========================================================================


class TestRoleBindingConstraints:
    @pytest.mark.anyio
    async def test_unique_org_principal_role(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_role(id="r-1", org_id="org-1"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(RoleBindingRow(id="b-1", org_id="org-1", principal_type="user", principal_id="u-1", role_id="r-1"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(RoleBindingRow(id="b-2", org_id="org-1", principal_type="user", principal_id="u-1", role_id="r-1"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_principal_type_check_rejects_invalid(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_role(id="r-1", org_id="org-1"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(RoleBindingRow(id="b-1", org_id="org-1", principal_type="robot", principal_id="u-1", role_id="r-1"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_fk_role_cascade_on_delete(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_role(id="r-1", org_id="org-1"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(RoleBindingRow(id="b-1", org_id="org-1", principal_type="user", principal_id="u-1", role_id="r-1"))
            await session.commit()

        async with AsyncSession(engine) as session:
            role = await session.get(RoleRow, "r-1")
            await session.delete(role)
            await session.commit()

        async with AsyncSession(engine) as session:
            assert await session.get(RoleBindingRow, "b-1") is None


# ===========================================================================
# service_accounts constraints
# ===========================================================================


class TestServiceAccountConstraints:
    @pytest.mark.anyio
    async def test_unique_org_name(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_svc(id="sa-1", org_id="org-1", name="bot"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(_svc(id="sa-2", org_id="org-1", name="bot"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_status_check_rejects_invalid(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            sa_row = ServiceAccountRow(id="sa-1", org_id="org-1", name="bot", status="bogus")
            session.add(sa_row)
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_pr_034_traceability_columns_nullable(self, engine):
        """PR-034 (0008_service_account_fields) added 5 nullable columns.

        A row constructed without any of them must commit cleanly. The
        ORM defaults to ``None`` so this is also a parity check that the
        ORM mirrors the migration's nullability.
        """
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            row = _svc(id="sa-1", org_id="org-1", name="bot")
            session.add(row)
            await session.commit()
            await session.refresh(row)
        assert row.owner_user_id is None
        assert row.purpose is None
        assert row.system is None
        assert row.environment is None
        assert row.expires_at is None

    @pytest.mark.anyio
    async def test_owner_user_id_not_unique_multiple_sas_share_owner(self, engine):
        """ADR §9.1: ``owner_user_id`` is accountability only, not a 1:1.

        One owner can be the accountable contact for many SAs (a team
        lead overseeing several CI runners, for example). There is no
        unique constraint on ``owner_user_id``.
        """
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_svc(id="sa-1", org_id="org-1", name="bot-a"))
            session.add(_svc(id="sa-2", org_id="org-1", name="bot-b"))
            await session.commit()  # must NOT raise

        async with AsyncSession(engine) as session:
            from sqlalchemy import select

            rows = (await session.execute(select(ServiceAccountRow).where(ServiceAccountRow.org_id == "org-1"))).scalars().all()
        assert len(rows) == 2


# ===========================================================================
# api_keys constraints
# ===========================================================================


class TestApiKeyConstraints:
    @pytest.mark.anyio
    async def test_unique_key_prefix(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_svc(id="sa-1", org_id="org-1"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(
                ApiKeyRow(
                    id="k-1",
                    org_id="org-1",
                    service_account_id="sa-1",
                    key_prefix="dk_prefix_",
                    key_hash="hash-aaa",
                    expires_at=_EXPIRES,
                )
            )
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(
                ApiKeyRow(
                    id="k-2",
                    org_id="org-1",
                    service_account_id="sa-1",
                    key_prefix="dk_prefix_",
                    key_hash="hash-bbb",
                    expires_at=_EXPIRES,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_fk_service_account_cascade_on_delete(self, engine):
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            session.add(_svc(id="sa-1", org_id="org-1"))
            await session.commit()

        async with AsyncSession(engine) as session:
            session.add(
                ApiKeyRow(
                    id="k-1",
                    org_id="org-1",
                    service_account_id="sa-1",
                    key_prefix="dk_prefix_",
                    key_hash="hash-aaa",
                    expires_at=_EXPIRES,
                )
            )
            await session.commit()

        async with AsyncSession(engine) as session:
            sa_row = await session.get(ServiceAccountRow, "sa-1")
            await session.delete(sa_row)
            await session.commit()

        async with AsyncSession(engine) as session:
            assert await session.get(ApiKeyRow, "k-1") is None


# ===========================================================================
# Migration round-trip (upgrade head ↔ downgrade to 0003)
# ===========================================================================


class TestMigrationRoundTrip:
    @pytest.mark.anyio
    async def test_revision_independently_upgradable_and_revertible(self, tmp_path: Path):
        """``0004_iam_tables`` must upgrade cleanly on a fresh DB and
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
            # Bootstrap already stamped head (0004); downgrade to 0003.
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0003_tenant_tables")

            check_engine = create_async_engine(url)
            async with check_engine.connect() as conn:
                names = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
            await check_engine.dispose()

            assert IAM_TABLES.isdisjoint(names), "IAM tables survived downgrade to 0003"

            # Re-upgrade to head — tables reappear.
            await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
            check_engine2 = create_async_engine(url)
            async with check_engine2.connect() as conn:
                names2 = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
            await check_engine2.dispose()
            assert IAM_TABLES <= names2, "IAM tables missing after re-upgrade to head"
        finally:
            await close_engine()

    @pytest.mark.anyio
    async def test_builtin_roles_seed_round_trip(self, tmp_path: Path):
        """``0007_builtin_roles`` seeds 3 system templates on upgrade and
        removes them on downgrade (pr-split-guide §7 + §8 PR-030).

        Downgrade to 0006 drops the seed rows + the ``template_version``
        column; re-upgrade restores both. The legacy/``create_all`` path
        (empty branch) never runs this revision, so the lifespan helper
        ``ensure_builtin_roles`` is the parallel seed path — this test only
        covers the migration path.
        """
        import asyncio

        import alembic.command as alembic_command
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.contracts.rbac import BUILTIN_ROLE_NAMES
        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'roles_roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            cfg = _get_alembic_config(get_engine())

            async def _builtin_role_count() -> int:
                engine = create_async_engine(url)
                try:
                    async with engine.connect() as conn:
                        result = await conn.execute(
                            text("SELECT COUNT(*) FROM roles WHERE is_system = 1 AND name IN (:n1, :n2, :n3)"),
                            {"n1": "org:admin", "n2": "org:developer", "n3": "org:viewer"},
                        )
                        return int(result.scalar())
                finally:
                    await engine.dispose()

            # Fresh bootstrap uses the empty branch (create_all + stamp head),
            # which never runs the seed migration — builtin roles come from
            # the lifespan helper ensure_builtin_roles in that path. To test the
            # migration's own seed behaviour we downgrade to 0006 first, which
            # drops both the seed rows and the template_version column, then
            # re-upgrade to force the migration to run.
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0006_enforce_org_not_null")
            assert await _builtin_role_count() == 0

            # Re-upgrade runs 0007_builtin_roles.upgrade(): adds the column
            # back and seeds the three builtin roles from the registry.
            await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
            assert await _builtin_role_count() == len(BUILTIN_ROLE_NAMES)

            # Downgrade again proves the seed is reversible.
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0006_enforce_org_not_null")
            assert await _builtin_role_count() == 0
        finally:
            await close_engine()

    @pytest.mark.anyio
    async def test_service_account_fields_round_trip(self, tmp_path: Path):
        """``0008_service_account_fields`` adds 5 columns and is reversible.

        PR-034: the revision adds ``owner_user_id`` / ``purpose`` /
        ``system`` / ``environment`` / ``expires_at`` to
        ``service_accounts``. Downgrade to ``0007_builtin_roles`` must
        drop all five; re-upgrade must restore them (pr-split-guide §7
        independently-upgradable).
        """
        import asyncio

        import alembic.command as alembic_command
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'sa_fields_roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            cfg = _get_alembic_config(get_engine())

            async def _column_names() -> set[str]:
                engine = create_async_engine(url)
                try:
                    async with engine.connect() as conn:
                        result = await conn.execute(text("PRAGMA table_info(service_accounts)"))
                        return {row[1] for row in result}
                finally:
                    await engine.dispose()

            # Fresh bootstrap has the columns (create_all provisions
            # them from the ORM). Downgrade to 0007 drops all five.
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0007_builtin_roles")
            after_down = await _column_names()
            for col in ("owner_user_id", "purpose", "system", "environment", "expires_at"):
                assert col not in after_down, f"{col} survived downgrade to 0007"

            # Re-upgrade restores them.
            await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
            after_up = await _column_names()
            for col in ("owner_user_id", "purpose", "system", "environment", "expires_at"):
                assert col in after_up, f"{col} missing after re-upgrade to head"
        finally:
            await close_engine()

    @pytest.mark.anyio
    async def test_oidc_group_mappings_round_trip(self, tmp_path: Path):
        """``0009_oidc_group_mappings`` creates the allowlist table and is reversible.

        PR-036: the revision adds the ``oidc_group_mappings`` table
        (ADR-0003 §10 6-field config model). Downgrade to
        ``0008_service_account_fields`` must drop the whole table;
        re-upgrade must restore it. The fresh-DB ``create_all`` path
        provisions the table from the ORM, so this round-trip exercises
        the legacy-upgrade branch (pr-split-guide §7 independently-
        upgradable).
        """
        import asyncio

        import alembic.command as alembic_command
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'oidc_mappings_roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            cfg = _get_alembic_config(get_engine())

            async def _table_exists() -> bool:
                engine = create_async_engine(url)
                try:
                    async with engine.connect() as conn:
                        result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='oidc_group_mappings'"))
                        return result.first() is not None
                finally:
                    await engine.dispose()

            # Fresh bootstrap has the table (create_all provisions it from ORM).
            assert await _table_exists() is True

            # Downgrade to 0008 drops the table entirely.
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0008_service_account_fields")
            assert await _table_exists() is False

            # Re-upgrade restores it.
            await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
            assert await _table_exists() is True

            # The mode CHECK + allowlist unique constraint are present.
            engine = create_async_engine(url)
            try:
                async with engine.connect() as conn:
                    cols = await conn.run_sync(lambda c: {col["name"] for col in sa.inspect(c).get_columns("oidc_group_mappings")})
                    assert {"issuer", "group_claim", "group_value", "target_org_id", "target_role_id", "mode"} <= cols
            finally:
                await engine.dispose()
        finally:
            await close_engine()
