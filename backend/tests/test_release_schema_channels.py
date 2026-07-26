"""Schema + migration tests for the ``release_channels`` / ``release_events`` tables (PR-053).

Mirrors ``test_release_schema.py`` (the PR-050 sibling): reflects the migrated
schema, asserts the CHECK constraints / FK RESTRICT / cross-dialect UNIQUE
behaviour, and round-trips the migration (downgrade to 0012 → upgrade to head).

The cross-dialect UNIQUE is the load-bearing assertion: on SQLite the
``COALESCE(workspace_id, '_default')`` expression index must reject two rows
with ``workspace_id IS NULL`` and otherwise-identical (org, package, channel)
— this is the SQLite-equivalent of Postgres ``NULLS NOT DISTINCT`` (ADR §5,
data-model.md §6.4). A plain ``UNIQUE`` would treat NULLs as distinct and
allow the duplicate.

Channel IDs: ``ART-600`` series (schema layer).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.base import Base
from deerflow.persistence.release.model import (
    AgentPackageRow,
    ReleaseChannelRow,
    ReleaseEventRow,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def engine(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_engine, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'channel_schema.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_engine()
    finally:
        await close_engine()


async def _columns(conn, table: str) -> set[str]:
    return await conn.run_sync(lambda c: {col["name"] for col in sa.inspect(c).get_columns(table)})


async def _check_constraints(conn, table: str) -> dict[str, str]:
    """Return {constraint_name: sqltext} for the CHECK constraints on table."""
    return await conn.run_sync(lambda c: {ck["name"]: str(ck["sqltext"]) for ck in sa.inspect(c).get_check_constraints(table)})


# ``release_channels.package_id`` / ``current_version_id`` and
# ``release_events.channel_id`` / ``to_version_id`` are FK-RESTRICT against
# ``agent_packages`` / ``agent_versions``. The raw ``sa.table()`` inserts the
# CHECK-constraint tests use must still satisfy the parent tables' own NOT
# NULL + CHECK constraints, so seed valid parent rows first via the ORM.
_PKG_COLS = (
    "id",
    "org_id",
    "name",
    "display_name",
    "status",
    "row_version",
    "created_at",
    "updated_at",
)
_VER_COLS = (
    "id",
    "org_id",
    "package_id",
    "version",
    "digest",
    "status",
    "manifest",
    "size_bytes",
    "created_at",
)


async def _seed_package(conn, *, pkg_id: str, org_id: str, name: str | None = None) -> None:
    """Insert a valid AgentPackageRow so FK + parent CHECKs hold."""
    from datetime import UTC, datetime

    await conn.execute(
        sa.insert(AgentPackageRow).values(
            id=pkg_id,
            org_id=org_id,
            name=name or pkg_id,
            display_name=name or pkg_id,
            status="active",
            row_version=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )


async def _seed_channel_row(
    conn,
    *,
    ch_id: str,
    org_id: str,
    pkg_id: str,
    channel: str = "dev",
    workspace_id: str | None = None,
) -> None:
    """Raw insert for release_channels — used by tests that need to control every column."""
    from datetime import UTC, datetime

    await conn.execute(
        sa.insert(ReleaseChannelRow).values(
            id=ch_id,
            org_id=org_id,
            workspace_id=workspace_id,
            package_id=pkg_id,
            channel=channel,
            current_version_id=None,
            row_version=1,
            updated_by=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )


async def _seed_event_row(
    conn,
    *,
    ev_id: str,
    org_id: str,
    channel_id: str,
    to_version_id: str,
    action: str = "promote",
) -> None:
    from datetime import UTC, datetime

    await conn.execute(
        sa.insert(ReleaseEventRow).values(
            id=ev_id,
            org_id=org_id,
            channel_id=channel_id,
            from_version_id=None,
            to_version_id=to_version_id,
            action=action,
            actor_type="user",
            actor_id="u-test",
            reason=None,
            created_at=datetime.now(UTC),
        )
    )


# ---------------------------------------------------------------------------
# Table + column presence (ART-600)
# ---------------------------------------------------------------------------


class TestTablesExist:
    async def test_both_tables_in_metadata(self):
        assert "release_channels" in Base.metadata.tables
        assert "release_events" in Base.metadata.tables

    async def test_release_channels_columns(self, engine):
        async with engine.begin() as conn:
            cols = await _columns(conn, "release_channels")
        expected = {
            "id",
            "org_id",
            "workspace_id",
            "package_id",
            "channel",
            "current_version_id",
            "row_version",
            "updated_by",
            "created_at",
            "updated_at",
        }
        assert expected <= cols, f"missing: {expected - cols}"

    async def test_release_events_columns(self, engine):
        async with engine.begin() as conn:
            cols = await _columns(conn, "release_events")
        expected = {
            "id",
            "org_id",
            "channel_id",
            "from_version_id",
            "to_version_id",
            "action",
            "actor_type",
            "actor_id",
            "reason",
            "created_at",
        }
        assert expected <= cols, f"missing: {expected - cols}"


# ---------------------------------------------------------------------------
# CHECK constraints (ART-610)
# ---------------------------------------------------------------------------


class TestCheckConstraints:
    async def test_release_channels_channel_check(self, engine):
        async with engine.begin() as conn:
            cks = await _check_constraints(conn, "release_channels")
        assert "ck_release_channels_channel" in cks
        sqltext = cks["ck_release_channels_channel"]
        assert "dev" in sqltext and "staging" in sqltext and "prod" in sqltext

    async def test_release_events_action_check(self, engine):
        async with engine.begin() as conn:
            cks = await _check_constraints(conn, "release_events")
        assert "ck_release_events_action" in cks
        sqltext = cks["ck_release_events_action"]
        assert "promote" in sqltext and "rollback" in sqltext

    async def test_channel_check_rejects_unknown_value(self, engine):
        """The CHECK must reject a channel outside {dev, staging, prod}."""
        async with engine.begin() as conn:
            await _seed_package(conn, pkg_id="pkg-1", org_id="org-1")
        # Raw insert bypassing the ORM to control exactly the channel value.
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(sa.text("INSERT INTO release_channels (id, org_id, package_id, channel, row_version, created_at, updated_at) VALUES ('ch-1', 'org-1', 'pkg-1', 'qa', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"))

    async def test_event_check_rejects_unknown_action(self, engine):
        async with engine.begin() as conn:
            await _seed_package(conn, pkg_id="pkg-2", org_id="org-2")
            await _seed_channel_row(conn, ch_id="ch-2", org_id="org-2", pkg_id="pkg-2", channel="dev")
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(sa.text("INSERT INTO release_events (id, org_id, channel_id, to_version_id, action, created_at) VALUES ('ev-1', 'org-2', 'ch-2', 'v-1', 'deploy', '2026-01-01 00:00:00')"))


# ---------------------------------------------------------------------------
# Cross-dialect UNIQUE (ART-620) — the load-bearing NULLS-NOT-DISTINCT test
# ---------------------------------------------------------------------------


class TestCrossDialectUnique:
    """The COALESCE expression index on SQLite must reject workspace=NULL dups.

    This is the SQLite-equivalent of Postgres NULLS NOT DISTINCT (ADR §5,
    data-model.md §6.4). Verified manually upfront during PR-053 planning.
    """

    async def test_duplicate_null_workspace_rejected(self, engine):
        async with engine.begin() as conn:
            await _seed_package(conn, pkg_id="pkg-u1", org_id="org-u")
            await _seed_channel_row(conn, ch_id="ch-a", org_id="org-u", pkg_id="pkg-u1", channel="dev")
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await _seed_channel_row(conn, ch_id="ch-b", org_id="org-u", pkg_id="pkg-u1", channel="dev")

    async def test_different_workspace_allowed(self, engine):
        """Two channels for the same (org, package, channel) but different workspaces."""
        async with engine.begin() as conn:
            await _seed_package(conn, pkg_id="pkg-u2", org_id="org-u2")
            await _seed_channel_row(conn, ch_id="ch-ws-a", org_id="org-u2", pkg_id="pkg-u2", channel="dev", workspace_id="ws-1")
            await _seed_channel_row(conn, ch_id="ch-ws-b", org_id="org-u2", pkg_id="pkg-u2", channel="dev", workspace_id="ws-2")
            # NULL workspace distinct from ws-1/ws-2.
            await _seed_channel_row(conn, ch_id="ch-ws-none", org_id="org-u2", pkg_id="pkg-u2", channel="dev", workspace_id=None)

    async def test_different_channel_allowed_same_workspace(self, engine):
        async with engine.begin() as conn:
            await _seed_package(conn, pkg_id="pkg-u3", org_id="org-u3")
            await _seed_channel_row(conn, ch_id="ch-d", org_id="org-u3", pkg_id="pkg-u3", channel="dev", workspace_id=None)
            await _seed_channel_row(conn, ch_id="ch-s", org_id="org-u3", pkg_id="pkg-u3", channel="staging", workspace_id=None)
            await _seed_channel_row(conn, ch_id="ch-p", org_id="org-u3", pkg_id="pkg-u3", channel="prod", workspace_id=None)


# ---------------------------------------------------------------------------
# FK RESTRICT (ART-630)
# ---------------------------------------------------------------------------


class TestForeignKeyRestrict:
    async def test_channel_cannot_delete_package_with_channel(self, engine):
        """package_id FK is RESTRICT — a package with a channel cannot be hard-deleted."""
        async with engine.begin() as conn:
            await _seed_package(conn, pkg_id="pkg-fk1", org_id="org-fk")
            await _seed_channel_row(conn, ch_id="ch-fk1", org_id="org-fk", pkg_id="pkg-fk1", channel="dev")
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(sa.delete(AgentPackageRow).where(AgentPackageRow.id == "pkg-fk1"))

    async def test_event_cannot_delete_channel_with_event(self, engine):
        """channel_id FK is RESTRICT — a channel with an event cannot be hard-deleted."""
        async with engine.begin() as conn:
            await _seed_package(conn, pkg_id="pkg-fk2", org_id="org-fk2")
            await _seed_channel_row(conn, ch_id="ch-fk2", org_id="org-fk2", pkg_id="pkg-fk2", channel="dev")
            await _seed_event_row(conn, ev_id="ev-fk2", org_id="org-fk2", channel_id="ch-fk2", to_version_id="v-fk2")
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(sa.delete(ReleaseChannelRow).where(ReleaseChannelRow.id == "ch-fk2"))


# ---------------------------------------------------------------------------
# Migration round-trip (ART-640)
# ---------------------------------------------------------------------------


class TestMigrationRoundTrip:
    async def test_downgrade_to_0012_then_upgrade_recreates_tables(self, tmp_path: Path):
        """Downgrade to 0012 drops both channel tables; upgrade head recreates them."""
        from alembic import command
        from alembic.config import Config as AlembicConfig

        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'channel_roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            engine = get_engine()

            def _cfg() -> AlembicConfig:
                from deerflow.persistence.bootstrap import _get_alembic_config

                return _get_alembic_config(engine)

            # Both tables exist at head (0013).
            async with engine.begin() as conn:
                tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
            assert "release_channels" in tables
            assert "release_events" in tables

            # Downgrade to 0012 → tables gone.
            await asyncio.to_thread(lambda: command.downgrade(_cfg(), "0012_agent_artifacts"))
            async with engine.begin() as conn:
                tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
            assert "release_channels" not in tables
            assert "release_events" not in tables

            # Upgrade head → tables back, CHECK constraints survive.
            await asyncio.to_thread(lambda: command.upgrade(_cfg(), "head"))
            async with engine.begin() as conn:
                tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
                cks_channels = await _check_constraints(conn, "release_channels")
                cks_events = await _check_constraints(conn, "release_events")
            assert "release_channels" in tables
            assert "release_events" in tables
            assert "ck_release_channels_channel" in cks_channels
            assert "ck_release_events_action" in cks_events
        finally:
            await close_engine()
