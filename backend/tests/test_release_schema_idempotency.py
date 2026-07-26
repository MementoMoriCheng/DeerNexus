"""Schema + migration tests for the ``release_idempotency_records`` table (PR-055).

Mirrors ``test_release_schema_channels.py`` (the PR-053 sibling): reflects the
migrated schema, asserts the plain ``UNIQUE(org_id, idempotency_key)``
constraint (the concurrency fence for Idempotency-Key replay), and round-trips
the migration (downgrade to 0014 → upgrade to head).

The table is deliberately simpler than ``release_channels``:

* No FK — a replay record is self-contained (it carries its own serialized
  ``PromoteResponse``); replaying does not re-touch the channel tables, so FK
  would wrongly couple replay-record GC to channel lifecycle.
* No CHECK — the table stores opaque client keys + a response blob; there is
  no closed set to enforce.
* Plain UNIQUE (no nullable column participates) — no NULLS NOT DISTINCT /
  COALESCE dance is needed, unlike ``release_channels``.

Schema IDs: ``ART-1500`` series (schema layer, PR-055).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.release.model import ReleaseIdempotencyRecordRow

pytestmark = pytest.mark.anyio


@pytest.fixture
async def engine(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_engine, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'idem_schema.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_engine()
    finally:
        await close_engine()


async def _columns(conn, table: str) -> set[str]:
    return await conn.run_sync(lambda c: {col["name"] for col in sa.inspect(c).get_columns(table)})


async def _indexes(conn, table: str) -> set[str]:
    return await conn.run_sync(lambda c: {idx["name"] for idx in sa.inspect(c).get_indexes(table)})


async def _unique_constraints(conn, table: str) -> set[str]:
    return await conn.run_sync(
        lambda c: {u["name"] for u in sa.inspect(c).get_unique_constraints(table)},
    )


# ---------------------------------------------------------------------------
# Columns + table presence (ART-1500)
# ---------------------------------------------------------------------------


class TestSchema:
    async def test_table_exists_at_head(self, engine):
        async with engine.begin() as conn:
            tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
        assert "release_idempotency_records" in tables

    async def test_columns(self, engine):
        async with engine.begin() as conn:
            cols = await _columns(conn, "release_idempotency_records")
        expected = {
            "id",
            "org_id",
            "idempotency_key",
            "request_hash",
            "response_payload",
            "status_code",
            "created_at",
        }
        assert expected <= cols, f"missing: {expected - cols}"

    async def test_status_code_is_integer(self, engine):
        async with engine.begin() as conn:
            cols = await conn.run_sync(
                lambda c: {col["name"]: col for col in sa.inspect(c).get_columns("release_idempotency_records")},
            )
        # Integer across dialects (BigInteger would also work but the column
        # is sa.Integer() in the migration — HTTP status fits in 16 bits).
        # SQLite reflects it as the concrete INTEGER subclass; just assert it
        # is an Integer instance rather than a string/blob type.
        assert isinstance(cols["status_code"]["type"], sa.Integer)

    async def test_unique_constraint_present(self, engine):
        async with engine.begin() as conn:
            uqs = await _unique_constraints(conn, "release_idempotency_records")
        assert "uq_release_idempotency_org_key" in uqs

    async def test_indexes_present(self, engine):
        async with engine.begin() as conn:
            idxs = await _indexes(conn, "release_idempotency_records")
        assert "idx_release_idempotency_org" in idxs
        assert "idx_release_idempotency_org_key" in idxs


# ---------------------------------------------------------------------------
# UNIQUE(org_id, idempotency_key) — the concurrency fence (ART-1510)
# ---------------------------------------------------------------------------


class TestUniqueFence:
    async def test_duplicate_org_key_rejected(self, engine):
        """Same (org_id, idempotency_key) must collide — the replay fence."""
        async with engine.begin() as conn:
            await conn.execute(
                sa.insert(ReleaseIdempotencyRecordRow).values(
                    id="rec-1",
                    org_id="org-1",
                    idempotency_key="key-A",
                    request_hash="hash-1",
                    response_payload={"channel": {}},
                    status_code=200,
                    created_at=datetime.now(UTC),
                ),
            )
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    sa.insert(ReleaseIdempotencyRecordRow).values(
                        id="rec-2",
                        org_id="org-1",
                        idempotency_key="key-A",  # same org + key → collision
                        request_hash="hash-2",
                        response_payload={"channel": {}},
                        status_code=200,
                        created_at=datetime.now(UTC),
                    ),
                )

    async def test_same_key_different_org_allowed(self, engine):
        """Idempotency keys are Org-scoped — two Orgs may reuse the same key."""
        async with engine.begin() as conn:
            await conn.execute(
                sa.insert(ReleaseIdempotencyRecordRow).values(
                    id="rec-3",
                    org_id="org-A",
                    idempotency_key="shared-key",
                    request_hash="hash-A",
                    response_payload={},
                    status_code=200,
                    created_at=datetime.now(UTC),
                ),
            )
            await conn.execute(
                sa.insert(ReleaseIdempotencyRecordRow).values(
                    id="rec-4",
                    org_id="org-B",  # different org → allowed
                    idempotency_key="shared-key",
                    request_hash="hash-B",
                    response_payload={},
                    status_code=200,
                    created_at=datetime.now(UTC),
                ),
            )

    async def test_different_key_same_org_allowed(self, engine):
        """One Org may hold many distinct idempotency keys."""
        async with engine.begin() as conn:
            for i in range(3):
                await conn.execute(
                    sa.insert(ReleaseIdempotencyRecordRow).values(
                        id=f"rec-multi-{i}",
                        org_id="org-multi",
                        idempotency_key=f"key-{i}",
                        request_hash=f"hash-{i}",
                        response_payload={},
                        status_code=200,
                        created_at=datetime.now(UTC),
                    ),
                )
        async with engine.begin() as conn:
            count = await conn.scalar(
                sa.select(sa.func.count())
                .select_from(ReleaseIdempotencyRecordRow)
                .where(
                    ReleaseIdempotencyRecordRow.org_id == "org-multi",
                ),
            )
        assert count == 3


# ---------------------------------------------------------------------------
# create_all ↔ migrated parity (ART-1520)
# ---------------------------------------------------------------------------


class TestCreateAllMigratedParity:
    async def test_create_all_emits_unique_constraint(self, tmp_path: Path):
        """The ORM ``__table_args__`` UniqueConstraint must be emitted by create_all.

        This guarantees the test backend (create_all) and the migrated backend
        (alembic upgrade head) enforce the same fence — a replay test using
        create_all must observe the same UNIQUE collision as production.
        """
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.persistence.base import Base

        parity_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'parity.db'}")
        try:
            async with parity_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all, tables=[ReleaseIdempotencyRecordRow.__table__])
            async with parity_engine.begin() as conn:
                uqs = await _unique_constraints(conn, "release_idempotency_records")
        finally:
            await parity_engine.dispose()
        assert "uq_release_idempotency_org_key" in uqs


# ---------------------------------------------------------------------------
# Migration round-trip (ART-1530)
# ---------------------------------------------------------------------------


class TestMigrationRoundTrip:
    async def test_downgrade_to_0014_then_upgrade_recreates_table(self, tmp_path: Path):
        """Downgrade to 0014 drops the idempotency table; upgrade head recreates it."""
        from alembic import command
        from alembic.config import Config as AlembicConfig

        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'idem_roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            engine = get_engine()

            def _cfg() -> AlembicConfig:
                from deerflow.persistence.bootstrap import _get_alembic_config

                return _get_alembic_config(engine)

            # Table exists at head (0015).
            async with engine.begin() as conn:
                tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
            assert "release_idempotency_records" in tables

            # Downgrade to 0014 → table gone.
            await asyncio.to_thread(lambda: command.downgrade(_cfg(), "0014_catalog_entries"))
            async with engine.begin() as conn:
                tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
            assert "release_idempotency_records" not in tables

            # Upgrade head → table back, UNIQUE + indexes survive.
            await asyncio.to_thread(lambda: command.upgrade(_cfg(), "head"))
            async with engine.begin() as conn:
                tables = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
                uqs = await _unique_constraints(conn, "release_idempotency_records")
                idxs = await _indexes(conn, "release_idempotency_records")
            assert "release_idempotency_records" in tables
            assert "uq_release_idempotency_org_key" in uqs
            assert "idx_release_idempotency_org" in idxs
            assert "idx_release_idempotency_org_key" in idxs
        finally:
            await close_engine()
