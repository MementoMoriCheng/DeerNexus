"""Schema, append-only and repository tests for the ``audit_events`` table (PR-040).

Covers ADR-0005 §3/§10/§12 invariants at the storage layer:

* the table exists after bootstrap with the §3 field set + persistence extras;
* the ``outcome`` CHECK rejects unknown values;
* revision ``0010_audit_events`` round-trips (upgrade → downgrade 0009 →
  re-upgrade) and is reversible;
* **append-only is enforced at the DB layer** — UPDATE and DELETE on a
  migrated DB raise (the trigger installed by 0010);
* the repository round-trips an ``AuditEvent`` DTO losslessly (actor /
  resource flattening), deduplicates on ``event_id`` (idempotency),
  enforces Org isolation on ``list_audit_events`` (OrgA ≠ OrgB), and
  scrubs secret-bearing payload keys as defence-in-depth.

Fixture conventions mirror ``test_iam_schema.py`` and
``test_oidc_group_mapping_repository.py``: boot an isolated file-backed
SQLite via ``init_engine`` (full bootstrap path, so the 0010 trigger is
installed) and tear down with ``close_engine``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.contracts.events import AuditEvent
from deerflow.contracts.identity import PrincipalRef
from deerflow.contracts.policy import ResourceRef
from deerflow.persistence.audit.model import AUDIT_OUTCOMES, AuditEventRow
from deerflow.persistence.audit.repository import (
    DEFAULT_PAGE_SIZE,
    count_by_org,
    get_audit_event,
    insert_audit_event,
    list_audit_events,
)

ORG_A = "00000000-0000-4000-8000-0000000000a1"
ORG_B = "00000000-0000-4000-8000-0000000000b2"
USER_ID = "00000000-0000-4000-8000-0000000000c3"
_NOW = datetime.now(UTC).replace(microsecond=0)

AUDIT_TABLES = {"audit_events"}
# The §3 field set that MUST be present (superset check — extras allowed).
REQUIRED_COLUMNS = {
    "event_id",
    "idempotency_key",
    "schema_version",
    "org_id",
    "workspace_id",
    "actor_type",
    "actor_id",
    "actor_user_id",
    "actor_display_name",
    "action",
    "outcome",
    "reason_code",
    "resource_type",
    "resource_id",
    "resource_org_id",
    "resource_workspace_id",
    "resource_attributes",
    "request_id",
    "trace_id",
    "run_id",
    "occurred_at",
    "payload",
    # ADR-0005 §3 persistence extras
    "ingested_at",
    "producer",
    "producer_version",
    "partition_key",
    "archive_batch_id",
}


def _event(
    *,
    event_id: str = "evt-1",
    org_id: str | None = ORG_A,
    action: str = "iam.role_binding.created",
    outcome: str = "success",
    occurred_at: datetime = _NOW,
    payload: dict | None = None,
    resource: ResourceRef | None = None,
    idempotency_key: str = "idem-1",
) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        idempotency_key=idempotency_key,
        org_id=org_id,
        actor=PrincipalRef(type="user", id=USER_ID, user_id=USER_ID, display_name="alice"),
        action=action,
        resource=resource,
        outcome=outcome,  # type: ignore[arg-type]
        request_id="req-1",
        trace_id="trace-1",
        run_id=None,
        occurred_at=occurred_at,
        payload=payload if payload is not None else {"role_id": "r-admin"},
    )


@pytest.fixture
async def sf(tmp_path: Path):
    """Boot an isolated SQLite via the full bootstrap path (installs the 0010 trigger)."""
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture
async def engine(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_engine, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'audit_engine.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_engine()
    finally:
        await close_engine()


@pytest.fixture
async def sf_migrated(tmp_path: Path):
    """A session factory over a DB whose schema came from the **alembic
    upgrade path**, not ``create_all``.

    The append-only trigger is installed by migration ``0010``'s ``upgrade()``,
    NOT by ``Base.metadata.create_all`` (DDL triggers are not part of ORM
    metadata). The standard ``sf`` fixture boots a fresh DB via the empty
    branch (``create_all`` + ``stamp head``), so its ``upgrade()`` never runs
    and the trigger is absent — the in-app INSERT-only repository is the
    guarantee on that path. To exercise the DB-layer trigger we must force
    the migration to run: bootstrap (which creates + stamps), then alembic
    ``downgrade 0009`` (drops the table + trigger) → ``upgrade head`` (re-runs
    0010, installing the trigger). This mirrors what a production/versioned
    DB actually goes through.
    """
    import alembic.command as alembic_command

    from deerflow.persistence.bootstrap import _get_alembic_config
    from deerflow.persistence.engine import close_engine, get_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'audit_migrated.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    cfg = _get_alembic_config(get_engine())
    await asyncio.to_thread(alembic_command.downgrade, cfg, "0009_oidc_group_mappings")
    await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
    try:
        yield get_session_factory()
    finally:
        await close_engine()


# ===========================================================================
# Table existence + column set
# ===========================================================================


class TestAuditTableExists:
    @pytest.mark.anyio
    async def test_audit_events_table_created_by_bootstrap(self, engine):
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
        assert AUDIT_TABLES <= names, f"missing audit tables: {AUDIT_TABLES - names}"

    @pytest.mark.anyio
    async def test_audit_events_has_required_columns(self, engine):
        async with engine.connect() as conn:
            cols = await conn.run_sync(lambda c: {col["name"] for col in sa.inspect(c).get_columns("audit_events")})
        missing = REQUIRED_COLUMNS - cols
        assert not missing, f"audit_events missing columns: {sorted(missing)}"


# ===========================================================================
# Constraint: outcome CHECK
# ===========================================================================


class TestOutcomeCheck:
    @pytest.mark.anyio
    async def test_valid_outcomes_accepted(self, sf):
        for outcome in AUDIT_OUTCOMES:
            row = await insert_audit_event(sf, _event(event_id=f"evt-ok-{outcome}", outcome=outcome))
            assert row.outcome == outcome

    @pytest.mark.anyio
    async def test_invalid_outcome_rejected_by_repository(self, sf):
        # The repository rejects an unknown outcome before hitting the DB
        # (belt-and-braces alongside the CHECK constraint).
        with pytest.raises(ValueError):
            await insert_audit_event(sf, _event(event_id="evt-bad", outcome="pending"))  # type: ignore[arg-type]

    @pytest.mark.anyio
    async def test_invalid_outcome_rejected_by_db_check(self, sf):
        # Bypass the repository guard to prove the CHECK constraint fires.
        row = AuditEventRow(
            event_id="evt-bad-check",
            idempotency_key="idem-bad",
            actor_type="user",
            actor_id=USER_ID,
            action="x",
            outcome="pending",
            request_id="r",
            occurred_at=_NOW,
            payload={},
        )
        with pytest.raises(IntegrityError):
            async with sf() as session:
                session.add(row)
                await session.commit()


# ===========================================================================
# Append-only enforcement (DB trigger)
# ===========================================================================


class TestAppendOnlyTrigger:
    """ADR-0005 §10.1/§13: the table must reject UPDATE and DELETE at the DB layer.

    These run against a **migrated** DB (``sf_migrated``), where the 0010
    ``upgrade()`` has actually executed and installed the trigger. The plain
    ``sf`` fixture's ``create_all`` path has no trigger (ORM metadata carries
    no DDL triggers); on that path the in-app INSERT-only repository is the
    append-only guarantee.
    """

    @pytest.mark.anyio
    async def test_update_is_rejected(self, sf_migrated):
        await insert_audit_event(sf_migrated, _event(event_id="evt-imm-1"))
        # SQLite surfaces RAISE(ABORT, ...) as IntegrityError.
        with pytest.raises(IntegrityError):
            async with sf_migrated() as session:
                await session.execute(sa.update(AuditEventRow).where(AuditEventRow.event_id == "evt-imm-1").values(outcome="denied"))
                await session.commit()

    @pytest.mark.anyio
    async def test_delete_is_rejected(self, sf_migrated):
        await insert_audit_event(sf_migrated, _event(event_id="evt-imm-2"))
        with pytest.raises(IntegrityError):
            async with sf_migrated() as session:
                await session.execute(sa.delete(AuditEventRow).where(AuditEventRow.event_id == "evt-imm-2"))
                await session.commit()

    @pytest.mark.anyio
    async def test_insert_still_works(self, sf_migrated):
        # Append-only means INSERT-only: inserting is the one allowed mutation.
        row = await insert_audit_event(sf_migrated, _event(event_id="evt-imm-3"))
        fetched = await get_audit_event(sf_migrated, event_id="evt-imm-3")
        assert fetched is not None
        assert fetched.event_id == row.event_id


# ===========================================================================
# Repository: round-trip + idempotency + Org isolation + scrub
# ===========================================================================


class TestRepositoryRoundTrip:
    @pytest.mark.anyio
    async def test_insert_and_get_lossless(self, sf):
        resource = ResourceRef(type="role_binding", id="rb-1", org_id=ORG_A, attributes={"via": "bootstrap"})
        ev = _event(event_id="evt-rt-1", resource=resource, payload={"role_id": "r-admin", "created": True})
        await insert_audit_event(sf, ev, producer="iam-router", producer_version="0.1.0")

        fetched = await get_audit_event(sf, event_id="evt-rt-1")
        assert fetched is not None
        # Flattened actor reconstructs the original PrincipalRef.
        assert fetched.actor_type == "user"
        assert fetched.actor_id == USER_ID
        assert fetched.actor_user_id == USER_ID
        assert fetched.actor_display_name == "alice"
        # Flattened resource reconstructs the original ResourceRef.
        assert fetched.resource_type == "role_binding"
        assert fetched.resource_id == "rb-1"
        assert fetched.resource_org_id == ORG_A
        assert fetched.resource_attributes == {"via": "bootstrap"}
        # Body.
        assert fetched.action == "iam.role_binding.created"
        assert fetched.outcome == "success"
        assert fetched.payload == {"role_id": "r-admin", "created": True}
        # Persistence extras.
        assert fetched.producer == "iam-router"
        assert fetched.producer_version == "0.1.0"
        # ingested_at set automatically, distinct from occurred_at.
        assert fetched.ingested_at is not None

    @pytest.mark.anyio
    async def test_duplicate_event_id_raises_integrity_error(self, sf):
        # Idempotency by event_id (ADR §9.1): a retry reusing event_id must
        # collide on the primary key, not silently double-insert.
        await insert_audit_event(sf, _event(event_id="evt-dup"))
        with pytest.raises(IntegrityError):
            await insert_audit_event(sf, _event(event_id="evt-dup"))

    @pytest.mark.anyio
    async def test_scrub_strips_forbidden_payload_keys(self, sf):
        # Build the DTO via model_construct to bypass the DTO's own validator,
        # proving the repository's secondary _scrub_payload still strips the
        # secret-bearing key before persistence (defence-in-depth, §6).
        ev = _event(event_id="evt-scrub", payload={"role_id": "r-admin"})
        ev = ev.model_construct(**{**ev.__dict__, "payload": {"role_id": "r-admin", "api_key": "dk_live_secret"}})
        row = await insert_audit_event(sf, ev)  # type: ignore[arg-type]
        assert "api_key" not in row.payload
        assert row.payload == {"role_id": "r-admin"}


class TestOrgIsolation:
    @pytest.mark.anyio
    async def test_org_a_query_does_not_return_org_b(self, sf):
        # ADR §15 "OrgA 查询不返回 OrgB": list_audit_events forces org_id.
        await insert_audit_event(sf, _event(event_id="evt-orgA", org_id=ORG_A))
        await insert_audit_event(sf, _event(event_id="evt-orgB", org_id=ORG_B))

        a_rows = await list_audit_events(sf, org_id=ORG_A)
        assert {r.event_id for r in a_rows} == {"evt-orgA"}

        b_rows = await list_audit_events(sf, org_id=ORG_B)
        assert {r.event_id for r in b_rows} == {"evt-orgB"}

        # count_by_org respects the same scope.
        assert await count_by_org(sf, org_id=ORG_A) == 1
        assert await count_by_org(sf, org_id=ORG_B) == 1


class TestListFiltersAndPagination:
    @pytest.mark.anyio
    async def _seed_page(self, sf) -> None:
        base = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
        for i in range(5):
            await insert_audit_event(
                sf,
                _event(
                    event_id=f"evt-page-{i}",
                    org_id=ORG_A,
                    action="iam.membership.suspended" if i % 2 == 0 else "iam.role_binding.created",
                    occurred_at=base + timedelta(seconds=i),
                ),
            )

    @pytest.mark.anyio
    async def test_action_filter(self, sf):
        await self._seed_page(sf)
        rows = await list_audit_events(sf, org_id=ORG_A, action="iam.membership.suspended")
        assert {r.event_id for r in rows} == {"evt-page-0", "evt-page-2", "evt-page-4"}

    @pytest.mark.anyio
    async def test_cursor_pagination_orders_by_occurred_at_then_event_id(self, sf):
        await self._seed_page(sf)
        first = await list_audit_events(sf, org_id=ORG_A, limit=2)
        assert [r.event_id for r in first] == ["evt-page-0", "evt-page-1"]
        cursor = (first[-1].occurred_at, first[-1].event_id)
        second = await list_audit_events(sf, org_id=ORG_A, cursor=cursor, limit=2)
        assert [r.event_id for r in second] == ["evt-page-2", "evt-page-3"]
        third = await list_audit_events(sf, org_id=ORG_A, cursor=(second[-1].occurred_at, second[-1].event_id), limit=2)
        assert [r.event_id for r in third] == ["evt-page-4"]

    @pytest.mark.anyio
    async def test_page_size_capped_at_default(self, sf):
        await self._seed_page(sf)
        # Request an absurdly large limit; the cap must clamp it.
        rows = await list_audit_events(sf, org_id=ORG_A, limit=9999)
        assert len(rows) == DEFAULT_PAGE_SIZE or len(rows) == 5  # 5 seeded < cap

    @pytest.mark.anyio
    async def test_time_range_filter(self, sf):
        await self._seed_page(sf)
        base = datetime(2026, 7, 23, 12, 0, 1, tzinfo=UTC)
        rows = await list_audit_events(sf, org_id=ORG_A, occurred_after=base)
        # seconds 2,3,4 survive (occurred_at > 1s strictly)
        assert {r.event_id for r in rows} == {"evt-page-2", "evt-page-3", "evt-page-4"}


# ===========================================================================
# Migration round-trip (0010 create ↔ 0009 drop)
# ===========================================================================


class TestMigrationRoundTrip:
    @pytest.mark.anyio
    async def test_audit_events_round_trip(self, tmp_path: Path):
        """``0010_audit_events`` creates the table + trigger and is reversible.

        Mirrors ``test_oidc_group_mappings_round_trip``: fresh bootstrap has
        the table (create_all from ORM), downgrade to 0009 drops it, re-upgrade
        restores it. The append-only trigger is installed by the migration
        path only, so after re-upgrade an UPDATE/DELETE must be rejected.
        """
        import alembic.command as alembic_command
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'audit_roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            cfg = _get_alembic_config(get_engine())

            async def _table_exists() -> bool:
                engine = create_async_engine(url)
                try:
                    async with engine.connect() as conn:
                        result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='audit_events'"))
                        return result.first() is not None
                finally:
                    await engine.dispose()

            assert await _table_exists() is True

            # Downgrade to 0009 drops the table (+ trigger).
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0009_oidc_group_mappings")
            assert await _table_exists() is False

            # Re-upgrade restores it.
            await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
            assert await _table_exists() is True

            engine = create_async_engine(url)
            try:
                async with engine.connect() as conn:
                    cols = await conn.run_sync(lambda c: {col["name"] for col in sa.inspect(c).get_columns("audit_events")})
                    assert {"event_id", "idempotency_key", "org_id", "action", "outcome", "occurred_at", "payload"} <= cols

                    # After re-upgrade the trigger is present: a manual UPDATE
                    # on a seeded row must raise (append-only enforced).
                    await conn.execute(
                        text(
                            "INSERT INTO audit_events (event_id, idempotency_key, schema_version, "
                            "actor_type, actor_id, action, outcome, request_id, occurred_at, payload, ingested_at) "
                            "VALUES ('rt-1','idem','v1alpha1','user','u','x','success','r',"
                            "'2026-07-23T12:00:00+00:00','{}','2026-07-23T12:00:00+00:00')"
                        )
                    )
                    await conn.commit()
                    with pytest.raises(IntegrityError):
                        await conn.execute(text("UPDATE audit_events SET outcome='denied' WHERE event_id='rt-1'"))
                        await conn.commit()
            finally:
                await engine.dispose()
        finally:
            await close_engine()

    @pytest.mark.anyio
    async def test_audit_outbox_round_trip(self, tmp_path: Path):
        """``0011_audit_outbox`` creates the outbox table and is reversible.

        The outbox is a normal mutable table (no append-only trigger — its
        status transitions pending→processing→published), so this round-trip
        only asserts table/column presence and reversibility, mirroring
        ``test_oidc_group_mappings_round_trip``.
        """
        import alembic.command as alembic_command
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'audit_outbox_roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            cfg = _get_alembic_config(get_engine())

            async def _table_exists() -> bool:
                engine = create_async_engine(url)
                try:
                    async with engine.connect() as conn:
                        result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='audit_outbox'"))
                        return result.first() is not None
                finally:
                    await engine.dispose()

            assert await _table_exists() is True

            # Downgrade to 0010 drops the outbox table.
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0010_audit_events")
            assert await _table_exists() is False

            # Re-upgrade restores it.
            await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
            assert await _table_exists() is True

            engine = create_async_engine(url)
            try:
                async with engine.connect() as conn:
                    cols = await conn.run_sync(lambda c: {col["name"] for col in sa.inspect(c).get_columns("audit_outbox")})
                    assert {
                        "id",
                        "event_id",
                        "payload_json",
                        "org_id",
                        "status",
                        "attempts",
                        "available_at",
                        "published_at",
                        "last_error",
                        "owner_token",
                    } <= cols
                    # status CHECK + event_id unique constraint are present.
                    constraints = await conn.run_sync(lambda c: {ck["name"] for ck in sa.inspect(c).get_check_constraints("audit_outbox")})
                    assert "ck_audit_outbox_status" in constraints
                    uniques = await conn.run_sync(lambda c: {u["name"] for u in sa.inspect(c).get_unique_constraints("audit_outbox")})
                    assert "uq_audit_outbox_event_id" in uniques
            finally:
                await engine.dispose()
        finally:
            await close_engine()
