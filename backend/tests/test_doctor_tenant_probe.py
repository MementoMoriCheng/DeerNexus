"""Tests for the live-DB tenant migration-phase probe (PR-025C).

Pins the runbook §5.2 state table end-to-end: each (phase, NULL-org-count,
org-count) triple must classify to the documented PASS/WARN/FAIL. The probe
reads two facts from a real (isolated SQLite) DB and one from config, so these
tests boot a throwaway DB, seed rows, point the probe at it, and assert the
classification — covering the two unconditional runbook FAIL rows (Feature ON
+ residual NULL; filter off + multi-Org) plus the WARN (active with one Org).

Follows the fixture conventions of ``test_backfill_default_org.py`` /
``test_default_org_bootstrap.py``: isolated file-backed SQLite via
``init_engine`` / ``close_engine``, with ``bootstrap_schema`` bringing the
tenant + resource tables into existence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from app.doctor.models import DoctorStatus
from app.doctor.tenant_probe import _classify, probe_tenant_migration_phase
from deerflow.persistence.orgs.model import OrganizationRow


@pytest.fixture
async def db(tmp_path: Path):
    """Boot an isolated SQLite DB at head schema; yield (url, sqlite_dir).

    The DB file is ``{sqlite_dir}/deerflow.db`` — the fixed filename
    ``DatabaseConfig.sqlite_path`` derives from ``sqlite_dir`` — so the probe's
    ``config.database.app_sqlalchemy_url`` resolves to exactly this file when
    the test builds a DatabaseConfig with the same ``sqlite_dir``.
    """
    from deerflow.persistence.engine import close_engine, init_engine

    sqlite_dir = str(tmp_path)
    # DatabaseConfig.sqlite_path == "{sqlite_dir}/deerflow.db"; mirror that.
    url = f"sqlite+aiosqlite:///{tmp_path / 'deerflow.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=sqlite_dir)
    try:
        yield url, sqlite_dir
    finally:
        await close_engine()


def _config_pointing_at(sqlite_dir: str):
    """Build a minimal AppConfig whose DB resolves to the fixture's file.

    ``DatabaseConfig.sqlite_path`` is a derived property ``{sqlite_dir}/
    deerflow.db``, so passing the same ``sqlite_dir`` the fixture used makes
    ``config.database.app_sqlalchemy_url`` point at the seeded DB.
    """
    from deerflow.config.app_config import AppConfig

    return AppConfig(
        sandbox={"use": "LocalSandboxProvider"},
        database={"backend": "sqlite", "sqlite_dir": sqlite_dir},
    )


async def _seed_org(db_url: str, *, org_id: str, slug: str, name: str) -> None:
    """Insert one Organization row via a fresh engine session."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(db_url)
    try:
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            session.add(OrganizationRow(id=org_id, slug=slug, name=name, status="active"))
            await session.commit()
    finally:
        await engine.dispose()


def _set_phase(monkeypatch, phase: str) -> None:
    """Point the probe's phase read at a fixed value."""
    monkeypatch.setattr(
        "app.doctor.tenant_probe.current_multi_org_phase",
        lambda: phase,
    )


# ===========================================================================
# Pure classifier (no DB) — pins the runbook §5.2 table
# ===========================================================================


class TestClassifyTable:
    def test_disabled_single_org_pass(self):
        assert _classify("disabled", null_org_total=5, org_count=1).status is DoctorStatus.PASS

    def test_disabled_multi_org_fail(self):
        # runbook §5.2 row 5: 租户过滤关闭但存在多 Org
        r = _classify("disabled", null_org_total=0, org_count=2)
        assert r.status is DoctorStatus.FAIL
        assert "disabled" in r.message

    def test_validation_clean_pass(self):
        assert _classify("validation", null_org_total=0, org_count=2).status is DoctorStatus.PASS

    def test_validation_residual_null_fail(self):
        r = _classify("validation", null_org_total=3, org_count=2)
        assert r.status is DoctorStatus.FAIL

    def test_validation_no_orgs_fail(self):
        assert _classify("validation", null_org_total=0, org_count=0).status is DoctorStatus.FAIL

    def test_active_clean_multi_org_pass(self):
        assert _classify("active", null_org_total=0, org_count=2).status is DoctorStatus.PASS

    def test_active_residual_null_fail(self):
        # runbook §5.2 row 4: Feature ON 但仍有空 org_id
        r = _classify("active", null_org_total=1, org_count=2)
        assert r.status is DoctorStatus.FAIL

    def test_active_single_org_warn(self):
        assert _classify("active", null_org_total=0, org_count=1).status is DoctorStatus.WARN


# ===========================================================================
# Live probe against an isolated DB
# ===========================================================================


class TestProbeLive:
    @pytest.mark.anyio
    async def test_disabled_one_org_pass(self, db, monkeypatch):
        url, sqlite_dir = db
        await _seed_org(url, org_id="default", slug="default", name="Default")
        _set_phase(monkeypatch, "disabled")
        result = await probe_tenant_migration_phase(_config_pointing_at(sqlite_dir))
        assert result.status is DoctorStatus.PASS
        assert result.check_id == "tenant.migration_state"

    @pytest.mark.anyio
    async def test_disabled_two_orgs_fail(self, db, monkeypatch):
        url, sqlite_dir = db
        await _seed_org(url, org_id="default", slug="default", name="Default")
        await _seed_org(url, org_id="second", slug="second", name="Second")
        _set_phase(monkeypatch, "disabled")
        result = await probe_tenant_migration_phase(_config_pointing_at(sqlite_dir))
        assert result.status is DoctorStatus.FAIL

    @pytest.mark.anyio
    async def test_validation_clean_pass(self, db, monkeypatch):
        url, sqlite_dir = db
        await _seed_org(url, org_id="default", slug="default", name="Default")
        await _seed_org(url, org_id="validation", slug="validation", name="Validation")
        _set_phase(monkeypatch, "validation")
        result = await probe_tenant_migration_phase(_config_pointing_at(sqlite_dir))
        assert result.status is DoctorStatus.PASS

    @pytest.mark.anyio
    async def test_active_two_orgs_pass(self, db, monkeypatch):
        url, sqlite_dir = db
        await _seed_org(url, org_id="default", slug="default", name="Default")
        await _seed_org(url, org_id="validation", slug="validation", name="Validation")
        _set_phase(monkeypatch, "active")
        result = await probe_tenant_migration_phase(_config_pointing_at(sqlite_dir))
        assert result.status is DoctorStatus.PASS


# ===========================================================================
# DB-failure containment — probe must FAIL, never raise
# ===========================================================================


class TestProbeFailureContainment:
    @pytest.mark.anyio
    async def test_unreachable_db_returns_fail_not_raise(self, tmp_path, monkeypatch):
        _set_phase(monkeypatch, "disabled")
        # Point at a path whose parent does not exist AND a backend that will
        # fail to connect — postgres with a bogus URL is the cleanest way to
        # force a connection error without depending on sqlite file semantics.
        from deerflow.config.app_config import AppConfig

        config = AppConfig(
            sandbox={"use": "LocalSandboxProvider"},
            database={
                "backend": "postgres",
                "postgres_url": "postgresql://nonexistent.invalid:1/deernexus",
            },
        )
        result = await probe_tenant_migration_phase(config)
        assert result.status is DoctorStatus.FAIL
        assert "Could not query" in result.message
        assert result.remediation is not None


# ===========================================================================
# No secret leakage in the report payload
# ===========================================================================


class TestNoSecretLeakage:
    @pytest.mark.anyio
    async def test_probe_message_has_no_db_url(self, db, monkeypatch):
        url, sqlite_dir = db
        await _seed_org(url, org_id="default", slug="default", name="Default")
        _set_phase(monkeypatch, "disabled")
        import dataclasses

        result = await probe_tenant_migration_phase(_config_pointing_at(sqlite_dir))
        payload = dataclasses.asdict(result)
        # The DB URL must never appear in any field of the check result.
        assert url not in str(payload)
        assert "aiosqlite" not in str(payload)
