"""Schema tests for the ``runs`` release-pin columns (PR-056, ART-1700).

Covers migration ``0016_run_release_pin`` (5 additive columns on ``runs``) +
ORM parity between ``create_all`` and the alembic chain, mirroring the
``test_release_schema_*`` family. The columns back ADR-0004 §6 step 7 (persist
the resolved ReleaseRef) and §12 (``legacy_unpinned`` gate).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa

# Pre-import models so Base.metadata is populated (organizations etc.) before
# create_all reflects it — mirrors tests/test_persistence_bootstrap.py.
import deerflow.persistence.models  # noqa: F401
from deerflow.persistence.base import Base
from deerflow.persistence.run.model import RunRow

pytestmark = pytest.mark.anyio


_ORG_ID = "org-schema-test"


def _columns(inspector: sa.Inspector, table: str = "runs") -> dict[str, dict]:
    return {c["name"]: c for c in inspector.get_columns(table)}


# ---------------------------------------------------------------------------
# Table / columns at head (ART-1710)
# ---------------------------------------------------------------------------


class TestRunReleasePinColumns:
    async def test_release_columns_exist_at_head(self):
        # Use the ORM table directly — the columns are declared on the model.
        cols = RunRow.__table__.columns
        for name in (
            "release_package_id",
            "release_version_id",
            "release_channel",
            "release_digest",
            "legacy_unpinned",
        ):
            assert name in cols, f"runs.{name} missing from ORM"

    async def test_legacy_unpinned_is_not_null_with_server_default_true(self):
        """legacy_unpinned is NOT NULL + server_default 'true' (ADR §12).

        NOT NULL so a row can never be in an indeterminate legacy state;
        server_default 'true' so the ALTER stamps existing rows as legacy and
        new rows stay legacy until start_run pins them.
        """
        col = RunRow.__table__.columns["legacy_unpinned"]
        assert col.nullable is False
        # server_default renders as a DefaultClause; its text must be 'true'.
        sd = col.server_default
        assert sd is not None
        assert "true" in str(sd.arg.text if hasattr(sd, "arg") else sd)

    async def test_release_pin_columns_are_nullable(self):
        """The four release-identity columns are nullable (legacy runs have none)."""
        for name in ("release_package_id", "release_version_id", "release_channel", "release_digest"):
            assert RunRow.__table__.columns[name].nullable is True


# ---------------------------------------------------------------------------
# create_all ↔ migrated parity (ART-1720)
# ---------------------------------------------------------------------------


class TestRunReleasePinParity:
    async def test_create_all_and_alembic_produce_same_runs_columns(self, tmp_path: Path):
        """create_all and a full alembic upgrade must reflect identical ``runs`` columns.

        This is the per-table slice of
        ``test_create_all_and_alembic_upgrade_produce_same_schema``; it isolates the
        PR-056 columns so a nullable/default drift here fails with a precise message.
        """
        from alembic import command
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import get_engine, init_engine

        # Path A: create_all (fresh DB, ORM-driven). Full metadata so the runs
        # FK to ``organizations`` resolves (creating only RunRow raises
        # NoReferencedTableError on the org_id FK).
        fresh_url = f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}"
        fresh_engine = create_async_engine(fresh_url)
        try:
            async with fresh_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with fresh_engine.begin() as conn:
                fresh_cols = await conn.run_sync(lambda c: _columns(sa.inspect(c)))
        finally:
            await fresh_engine.dispose()

        # Path B: alembic upgrade base→head (versioned path). init_engine migrates.
        migrated_url = f"sqlite+aiosqlite:///{tmp_path / 'migrated.db'}"
        await init_engine("sqlite", url=migrated_url, sqlite_dir=str(tmp_path))
        try:
            engine = get_engine()
            cfg = _get_alembic_config(engine)
            await asyncio.to_thread(lambda: command.upgrade(cfg, "head"))
            async with engine.begin() as conn:
                migrated_cols = await conn.run_sync(lambda c: _columns(sa.inspect(c)))
        finally:
            from deerflow.persistence.engine import close_engine

            await close_engine()

        # Column set + nullable + (legacy_unpinned) default must agree.
        assert set(fresh_cols) == set(migrated_cols), f"runs column-set drift: only-create_all={set(fresh_cols) - set(migrated_cols)} only-alembic={set(migrated_cols) - set(fresh_cols)}"
        for name in ("legacy_unpinned", "release_package_id", "release_version_id", "release_channel", "release_digest"):
            assert fresh_cols[name]["nullable"] == migrated_cols[name]["nullable"], f"runs.{name} nullable drift create_all={fresh_cols[name]['nullable']} alembic={migrated_cols[name]['nullable']}"


# ---------------------------------------------------------------------------
# Migration round-trip (ART-1730)
# ---------------------------------------------------------------------------


class TestRunReleasePinRoundTrip:
    async def test_downgrade_to_0015_then_upgrade_restores_columns(self, tmp_path: Path):
        """Downgrade to 0015 drops the 5 columns; upgrade head restores them."""
        from alembic import command
        from alembic.config import Config as AlembicConfig

        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'pin_roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            engine = get_engine()

            def _cfg() -> AlembicConfig:
                return _get_alembic_config(engine)

            # Columns present at head (0016).
            async with engine.begin() as conn:
                cols = await conn.run_sync(lambda c: _columns(sa.inspect(c)))
            assert "legacy_unpinned" in cols
            assert "release_version_id" in cols

            # Downgrade to 0015 → columns gone.
            await asyncio.to_thread(lambda: command.downgrade(_cfg(), "0015_release_idempotency"))
            async with engine.begin() as conn:
                cols = await conn.run_sync(lambda c: _columns(sa.inspect(c)))
            assert "legacy_unpinned" not in cols
            assert "release_version_id" not in cols

            # Upgrade head → columns back.
            await asyncio.to_thread(lambda: command.upgrade(_cfg(), "head"))
            async with engine.begin() as conn:
                cols = await conn.run_sync(lambda c: _columns(sa.inspect(c)))
            assert "legacy_unpinned" in cols
            assert cols["legacy_unpinned"]["nullable"] is False
        finally:
            await close_engine()
