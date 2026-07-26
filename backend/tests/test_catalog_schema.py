"""Schema + migration tests for the ``catalog_entries`` discovery-index table (PR-054).

Mirrors ``test_release_schema_channels.py``: reflects the migrated schema,
asserts the CHECK constraints / UNIQUE, and round-trips the migration
(downgrade to 0013 → upgrade to head).

Catalog IDs: ``ART-1100`` series (schema layer).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.base import Base
from deerflow.persistence.catalog.model import CatalogEntryRow

pytestmark = pytest.mark.anyio


@pytest.fixture
async def engine(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_engine, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'catalog_schema.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_engine()
    finally:
        await close_engine()


async def _columns(conn, table: str) -> set[str]:
    return await conn.run_sync(lambda c: {col["name"] for col in sa.inspect(c).get_columns(table)})


async def _check_constraints(conn, table: str) -> dict[str, str]:
    return await conn.run_sync(lambda c: {ck["name"]: str(ck["sqltext"]) for ck in sa.inspect(c).get_check_constraints(table)})


async def _seed_row(
    conn,
    *,
    entry_id: str = "ce-1",
    org_id: str = "org-1",
    workspace_id: str | None = None,
    resource_type: str = "agent",
    resource_id: str = "res-1",
    name: str = "n",
    source: str = "database",
    status: str = "active",
) -> None:
    from datetime import UTC, datetime

    await conn.execute(
        sa.insert(CatalogEntryRow).values(
            id=entry_id,
            org_id=org_id,
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            name=name,
            display_name=name,
            source=source,
            status=status,
            metadata_={"k": "v"},
            synced_at=datetime.now(UTC),
        )
    )


# ---------------------------------------------------------------------------
# Table + column presence (ART-1100)
# ---------------------------------------------------------------------------


class TestTablesExist:
    async def test_table_in_metadata(self):
        assert "catalog_entries" in Base.metadata.tables

    async def test_columns(self, engine):
        async with engine.begin() as conn:
            cols = await _columns(conn, "catalog_entries")
        expected = {
            "id",
            "org_id",
            "workspace_id",
            "resource_type",
            "resource_id",
            "name",
            "display_name",
            "source",
            "status",
            "metadata",
            "synced_at",
        }
        assert expected <= cols, f"missing: {expected - cols}"


# ---------------------------------------------------------------------------
# CHECK constraints (ART-1110)
# ---------------------------------------------------------------------------


class TestCheckConstraints:
    async def test_all_three_checks_present(self, engine):
        async with engine.begin() as conn:
            cks = await _check_constraints(conn, "catalog_entries")
        assert "ck_catalog_entries_resource_type" in cks
        assert "ck_catalog_entries_source" in cks
        assert "ck_catalog_entries_status" in cks
        # Spot-check the closed sets are referenced.
        assert "agent" in cks["ck_catalog_entries_resource_type"]
        assert "file_import" in cks["ck_catalog_entries_source"]
        assert "active" in cks["ck_catalog_entries_status"]

    async def test_unknown_resource_type_rejected(self, engine):
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await _seed_row(conn, resource_type="workflow")

    async def test_unknown_source_rejected(self, engine):
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await _seed_row(conn, source="upload")

    async def test_unknown_status_rejected(self, engine):
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await _seed_row(conn, status="pending")


# ---------------------------------------------------------------------------
# UNIQUE (ART-1120)
# ---------------------------------------------------------------------------


class TestUnique:
    async def test_duplicate_org_resource_rejected(self, engine):
        async with engine.begin() as conn:
            await _seed_row(conn, entry_id="ce-a")
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await _seed_row(conn, entry_id="ce-b")  # same (org, type, resource_id)

    async def test_different_org_same_resource_allowed(self, engine):
        async with engine.begin() as conn:
            await _seed_row(conn, entry_id="ce-a", org_id="org-1")
            await _seed_row(conn, entry_id="ce-b", org_id="org-2")

    async def test_different_resource_type_same_org_allowed(self, engine):
        async with engine.begin() as conn:
            await _seed_row(conn, entry_id="ce-a", resource_type="agent")
            await _seed_row(conn, entry_id="ce-b", resource_type="skill")


# ---------------------------------------------------------------------------
# Migration round-trip (ART-1130)
# ---------------------------------------------------------------------------


class TestMigrationRoundTrip:
    async def test_downgrade_to_0013_then_upgrade_recreates_table(self, tmp_path: Path):
        from alembic import command
        from alembic.config import Config as AlembicConfig

        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'catalog_roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            engine = get_engine()

            def _cfg() -> AlembicConfig:
                from deerflow.persistence.bootstrap import _get_alembic_config

                return _get_alembic_config(engine)

            async with engine.begin() as conn:
                tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
            assert "catalog_entries" in tables

            await asyncio.to_thread(lambda: command.downgrade(_cfg(), "0013_release_channels_events"))
            async with engine.begin() as conn:
                tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
            assert "catalog_entries" not in tables

            await asyncio.to_thread(lambda: command.upgrade(_cfg(), "head"))
            async with engine.begin() as conn:
                tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
                cks = await _check_constraints(conn, "catalog_entries")
            assert "catalog_entries" in tables
            assert "ck_catalog_entries_resource_type" in cks
        finally:
            await close_engine()
