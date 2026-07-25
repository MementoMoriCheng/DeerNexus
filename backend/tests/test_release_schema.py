"""Constraint and migration tests for the agent-artifact tables (PR-050).

Verifies that the two tables introduced by revision ``0012_agent_artifacts``
(``agent_packages``, ``agent_versions``) exist after bootstrap, enforce
their declared constraints (CHECK / UNIQUE / FK RESTRICT), and round-trip
through ``alembic upgrade`` / ``downgrade``.

Follows the conventions of ``test_iam_schema.py`` (PR-020B sibling) and
``test_audit_schema.py`` (PR-040 sibling): each test boots an isolated
file-backed SQLite DB via ``init_engine`` (exercising the full bootstrap
path) and tears it down with ``close_engine``. DB-level constraints are
asserted by provoking ``IntegrityError`` with a manual insert.

Scope note (data-model.md §6.2/§6.3, ADR-0004 §3): this PR lands the tables
only. The ``published``-immutability freeze (content / manifest / digest /
version cannot change once ``status='published'``) is a write-path concern
enforced by the repository in PR-052 — it is intentionally **not** tested
here because there is no write caller in this PR.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.release.model import AgentPackageRow, AgentVersionRow

RELEASE_TABLES = {"agent_packages", "agent_versions"}

# data-model.md §6.2 / §6.3 — column set each table must expose.
PACKAGE_COLUMNS = {
    "id",
    "org_id",
    "workspace_id",
    "name",
    "display_name",
    "description",
    "status",
    "created_by",
    "created_at",
    "updated_at",
    "row_version",
}
VERSION_COLUMNS = {
    "id",
    "org_id",
    "package_id",
    "version",
    "digest",
    "status",
    "manifest",
    "content_inline",
    "object_key",
    "size_bytes",
    "created_by",
    "created_at",
    "published_at",
    "revoked_at",
}


def _pkg(
    *,
    id: str = "pkg-1",
    org_id: str = "org-1",
    name: str = "researcher",
    display_name: str = "Researcher",
    status: str = "active",
    workspace_id: str | None = None,
) -> AgentPackageRow:
    return AgentPackageRow(id=id, org_id=org_id, name=name, display_name=display_name, status=status, workspace_id=workspace_id)


def _ver(
    *,
    id: str = "ver-1",
    org_id: str = "org-1",
    package_id: str = "pkg-1",
    version: str = "1.0.0",
    digest: str = "sha256:aaaa",
    status: str = "draft",
    manifest: dict | None = None,
    content_inline: str | None = "hello",
    object_key: str | None = None,
    size_bytes: int = 5,
    published_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> AgentVersionRow:
    return AgentVersionRow(
        id=id,
        org_id=org_id,
        package_id=package_id,
        version=version,
        digest=digest,
        status=status,
        manifest=manifest if manifest is not None else {"agent_entry": "main"},
        content_inline=content_inline,
        object_key=object_key,
        size_bytes=size_bytes,
        published_at=published_at,
        revoked_at=revoked_at,
    )


@pytest.fixture
async def engine(tmp_path: Path):
    """Boot an isolated SQLite DB through the full bootstrap path."""
    from deerflow.persistence.engine import close_engine, get_engine, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'release.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_engine()
    finally:
        await close_engine()


# ===========================================================================
# Table existence & column set
# ===========================================================================


class TestTablesExist:
    @pytest.mark.anyio
    async def test_both_tables_created_by_bootstrap(self, engine):
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
        assert RELEASE_TABLES <= names, f"missing release tables: {RELEASE_TABLES - names}"

    @pytest.mark.anyio
    async def test_agent_packages_column_set(self, engine):
        async with engine.connect() as conn:
            cols = await conn.run_sync(lambda c: {col["name"] for col in sa.inspect(c).get_columns("agent_packages")})
        assert not (PACKAGE_COLUMNS - cols), f"missing agent_packages columns: {PACKAGE_COLUMNS - cols}"

    @pytest.mark.anyio
    async def test_agent_versions_column_set(self, engine):
        async with engine.connect() as conn:
            cols = await conn.run_sync(lambda c: {col["name"] for col in sa.inspect(c).get_columns("agent_versions")})
        assert not (VERSION_COLUMNS - cols), f"missing agent_versions columns: {VERSION_COLUMNS - cols}"


# ===========================================================================
# agent_packages constraints
# ===========================================================================


class TestPackageConstraints:
    @pytest.mark.anyio
    async def test_package_inserts_and_reads_back(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            await session.commit()
        async with AsyncSession(engine) as session:
            row = await session.get(AgentPackageRow, "pkg-1")
            assert row is not None
            assert row.org_id == "org-1"
            assert row.name == "researcher"
            assert row.status == "active"
            assert row.row_version == 1

    @pytest.mark.anyio
    async def test_status_check_rejects_unknown(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg(status="bogus"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_status_archived_allowed(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg(id="pkg-a", status="archived"))
            await session.commit()  # must NOT raise

    @pytest.mark.anyio
    async def test_unique_org_name(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg(id="p-1", org_id="org-1", name="dup"))
            await session.commit()
        async with AsyncSession(engine) as session:
            session.add(_pkg(id="p-2", org_id="org-1", name="dup"))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_different_orgs_allow_same_name(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg(id="p-1", org_id="org-1", name="shared"))
            session.add(_pkg(id="p-2", org_id="org-2", name="shared"))
            await session.commit()  # must NOT raise — Org-scoped uniqueness

    @pytest.mark.anyio
    async def test_workspace_id_is_optional(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg(id="p-ws", workspace_id=None))
            await session.commit()
        async with AsyncSession(engine) as session:
            row = await session.get(AgentPackageRow, "p-ws")
            assert row is not None
            assert row.workspace_id is None


# ===========================================================================
# agent_versions constraints — the immutable-content core
# ===========================================================================


class TestVersionStatusCheck:
    @pytest.mark.anyio
    async def test_draft_status_allowed(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(status="draft"))
            await session.commit()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "status",
        ["draft", "reviewed", "published", "revoked", "archived"],
    )
    async def test_all_state_machine_statuses_allowed(self, engine, status):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(id=f"v-{status}", version=f"1.0.{status}", digest=f"sha256:{status}", status=status))
            await session.commit()

    @pytest.mark.anyio
    async def test_status_check_rejects_unknown(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(status="approved"))  # not in the state machine
            with pytest.raises(IntegrityError):
                await session.commit()


class TestVersionContentExclusive:
    """content_inline XOR object_key (ADR-0004 §3.2 / data-model §6.3)."""

    @pytest.mark.anyio
    async def test_content_inline_only_allowed(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(content_inline="payload", object_key=None, size_bytes=7))
            await session.commit()

    @pytest.mark.anyio
    async def test_object_key_only_allowed(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(content_inline=None, object_key="org/x/v1/artifact", size_bytes=0))
            await session.commit()

    @pytest.mark.anyio
    async def test_both_null_rejected(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(content_inline=None, object_key=None))
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_both_set_rejected(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(content_inline="payload", object_key="org/x/v1/artifact"))
            with pytest.raises(IntegrityError):
                await session.commit()


class TestVersionSizeNonNegative:
    @pytest.mark.anyio
    async def test_zero_size_allowed(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(size_bytes=0, content_inline="", object_key=None))
            await session.commit()

    @pytest.mark.anyio
    async def test_negative_size_rejected(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(size_bytes=-1))
            with pytest.raises(IntegrityError):
                await session.commit()


class TestVersionUniqueConstraints:
    @pytest.mark.anyio
    async def test_unique_org_package_version(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(version="1.0.0", digest="sha256:aaa"))
            await session.commit()
        async with AsyncSession(engine) as session:
            session.add(_ver(id="v-2", version="1.0.0", digest="sha256:bbb"))  # same (org,pkg,version)
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_same_version_different_package_allowed(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg(id="pkg-1", name="alpha"))
            session.add(_pkg(id="pkg-2", name="beta"))
            session.add(_ver(package_id="pkg-1", version="1.0.0", digest="sha256:aaa"))
            session.add(_ver(id="v-2", package_id="pkg-2", version="1.0.0", digest="sha256:bbb"))
            await session.commit()  # different package → not a collision

    @pytest.mark.anyio
    async def test_unique_org_digest(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(version="1.0.0", digest="sha256:dup"))
            await session.commit()
        async with AsyncSession(engine) as session:
            session.add(_ver(id="v-2", version="2.0.0", digest="sha256:dup"))  # same (org,digest)
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_same_digest_different_org_allowed(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg(id="pkg-1", org_id="org-1", name="alpha"))
            session.add(_pkg(id="pkg-2", org_id="org-2", name="beta"))
            session.add(_ver(org_id="org-1", package_id="pkg-1", version="1.0.0", digest="sha256:shared"))
            session.add(_ver(id="v-2", org_id="org-2", package_id="pkg-2", version="1.0.0", digest="sha256:shared"))
            await session.commit()  # Org-scoped digest uniqueness


class TestVersionForeignKey:
    @pytest.mark.anyio
    async def test_package_delete_restricted_when_version_exists(self, engine):
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver())
            await session.commit()
        async with AsyncSession(engine) as session:
            pkg = await session.get(AgentPackageRow, "pkg-1")
            assert pkg is not None
            await s_delete(session, pkg)
            with pytest.raises(IntegrityError):
                await session.commit()

    @pytest.mark.anyio
    async def test_version_cascade_when_no_version(self, engine):
        """A package with NO versions can be deleted (RESTRICT does not block)."""
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            await session.commit()
        async with AsyncSession(engine) as session:
            pkg = await session.get(AgentPackageRow, "pkg-1")
            assert pkg is not None
            await s_delete(session, pkg)
            await session.commit()  # no versions → allowed
        async with AsyncSession(engine) as session:
            assert await session.get(AgentPackageRow, "pkg-1") is None


# ===========================================================================
# Content round-trip — the manifest/payload survives a write→read cycle
# ===========================================================================


class TestContentRoundTrip:
    @pytest.mark.anyio
    async def test_manifest_json_round_trip(self, engine):
        manifest = {
            "schema_version": "v1alpha1",
            "agent_entry": "main",
            "skills": [{"id": "skill-1", "version": "1.0.0"}],
            "tools": ["search"],
            "secret_requirements": [{"name": "API_KEY", "ref": "secret_ref"}],
        }
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(_ver(id="v-m", manifest=manifest, content_inline="body", object_key=None, size_bytes=4))
            await session.commit()
        async with AsyncSession(engine) as session:
            row = await session.get(AgentVersionRow, "v-m")
            assert row is not None
            assert row.manifest == manifest
            assert row.content_inline == "body"

    @pytest.mark.anyio
    async def test_timestamps_round_trip(self, engine):
        """``published_at`` / ``created_at`` survive a write→read cycle.

        SQLite's aiosqlite driver strips tzinfo on readback (a known platform
        difference — Postgres ``DateTime(timezone=True)`` preserves it), so
        this asserts the stored *value* matches, not the tzinfo flag. The
        ``created_at`` default fires server-side via the ORM default.
        """
        published = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC).replace(microsecond=0)
        async with AsyncSession(engine) as session:
            session.add(_pkg())
            session.add(
                _ver(
                    id="v-ts",
                    status="published",
                    content_inline="x",
                    object_key=None,
                    size_bytes=1,
                    published_at=published,
                )
            )
            await session.commit()
        async with AsyncSession(engine) as session:
            row = await session.get(AgentVersionRow, "v-ts")
            assert row is not None
            assert row.published_at is not None
            assert row.published_at.replace(tzinfo=None) == published.replace(tzinfo=None)
            assert row.created_at is not None  # ORM default fired


# ===========================================================================
# Constraint & index names are declared (relied on by backup-verify SKIP
# lists and doctor probes) — mirrors the audit_outbox round-trip pattern.
# ===========================================================================


class TestConstraintNamesDeclared:
    @pytest.mark.anyio
    async def test_agent_versions_check_constraints_named(self, engine):
        async with engine.connect() as conn:
            checks = await conn.run_sync(lambda c: {ck["name"] for ck in sa.inspect(c).get_check_constraints("agent_versions")})
        assert "ck_agent_versions_status" in checks
        assert "ck_agent_versions_content_exclusive" in checks
        assert "ck_agent_versions_size_nonneg" in checks

    @pytest.mark.anyio
    async def test_agent_versions_unique_constraints_named(self, engine):
        async with engine.connect() as conn:
            uniques = await conn.run_sync(lambda c: {u["name"] for u in sa.inspect(c).get_unique_constraints("agent_versions")})
        assert "uq_agent_versions_pkg_version" in uniques
        assert "uq_agent_versions_org_digest" in uniques

    @pytest.mark.anyio
    async def test_agent_packages_constraints_named(self, engine):
        async with engine.connect() as conn:
            checks = await conn.run_sync(lambda c: {ck["name"] for ck in sa.inspect(c).get_check_constraints("agent_packages")})
            uniques = await conn.run_sync(lambda c: {u["name"] for u in sa.inspect(c).get_unique_constraints("agent_packages")})
        assert "ck_agent_packages_status" in checks
        assert "uq_agent_packages_org_name" in uniques

    @pytest.mark.anyio
    async def test_package_fk_targets_agent_packages_with_restrict(self, engine):
        """``package_id`` references ``agent_packages.id`` (ON DELETE RESTRICT).

        SQLite's reflection does not surface the constraint *name* (it returns
        ``None``), so this asserts the target table/column instead — which is
        the cross-dialect-reliable property. The RESTRICT behaviour is
        exercised end-to-end by ``TestVersionForeignKey`` above.
        """
        async with engine.connect() as conn:
            fks = await conn.run_sync(lambda c: sa.inspect(c).get_foreign_keys("agent_versions"))
        package_fks = [f for f in fks if "package_id" in f.get("constrained_columns", [])]
        assert len(package_fks) == 1, f"expected one package_id FK, got {package_fks}"
        assert package_fks[0]["referred_table"] == "agent_packages"
        assert package_fks[0]["referred_columns"] == ["id"]


# ===========================================================================
# Migration round-trip — 0012 ↔ 0011 (clean drop + recreate)
# ===========================================================================


class TestMigrationRoundTrip:
    """``0012_agent_artifacts`` creates both tables and is reversible."""

    @pytest.mark.anyio
    async def test_tables_drop_on_downgrade_and_recreate_on_upgrade(self, tmp_path: Path):
        import alembic.command as alembic_command

        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'roundtrip.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            cfg = _get_alembic_config(get_engine())

            # Bootstrap stamps head (0012); both tables present.
            assert await _tables_exist(url)

            # Downgrade to 0011 drops both tables cleanly.
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0011_audit_outbox")
            assert not await _tables_exist(url), "release tables survived downgrade to 0011"

            # Re-upgrade recreates them.
            await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
            assert await _tables_exist(url), "release tables missing after upgrade to head"
        finally:
            await close_engine()

    @pytest.mark.anyio
    async def test_constraints_recreated_after_round_trip(self, tmp_path: Path):
        import alembic.command as alembic_command

        from deerflow.persistence.bootstrap import _get_alembic_config
        from deerflow.persistence.engine import close_engine, get_engine, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'roundtrip_constraints.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            cfg = _get_alembic_config(get_engine())
            await asyncio.to_thread(alembic_command.downgrade, cfg, "0011_audit_outbox")
            await asyncio.to_thread(alembic_command.upgrade, cfg, "head")

            eng = create_async_engine(url)
            try:
                async with eng.connect() as conn:
                    checks = await conn.run_sync(lambda c: {ck["name"] for ck in sa.inspect(c).get_check_constraints("agent_versions")})
                    uniques = await conn.run_sync(lambda c: {u["name"] for u in sa.inspect(c).get_unique_constraints("agent_versions")})
            finally:
                await eng.dispose()
            assert "ck_agent_versions_content_exclusive" in checks
            assert "uq_agent_versions_org_digest" in uniques
        finally:
            await close_engine()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def s_delete(session: AsyncSession, obj: object) -> None:
    """``session.delete`` wrapper that satisfies the type checker."""
    await session.delete(obj)  # type: ignore[arg-type]


async def _tables_exist(url: str) -> bool:
    eng = create_async_engine(url)
    try:
        async with eng.connect() as conn:
            names = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
    finally:
        await eng.dispose()
    return RELEASE_TABLES <= names
