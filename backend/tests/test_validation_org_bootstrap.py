"""Idempotency + audit tests for the validation-Org tenant bootstrap (PR-025B).

Verifies :func:`deerflow.tenancy.bootstrap.ensure_validation_org` is
idempotent, creates a valid ``organizations`` row, emits the right tenant
audit events, and (crucially) does **not** create Membership / RoleBinding —
the validation Org is inert until a later operator step. Also pins the
lifespan hook's phase-gating: ``disabled`` does not seed a validation Org.

Follows the fixture conventions of ``test_default_org_bootstrap.py``:
isolated file-backed SQLite via ``init_engine`` / ``close_engine``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import sqlalchemy as sa

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.iam.model import RoleBindingRow
from deerflow.persistence.orgs.model import OrganizationRow, OrgMembershipRow
from deerflow.tenancy import ensure_default_org, ensure_validation_org

DEFAULT_ORG_ID = "default"
VALIDATION_ORG_ID = "validation"
VALIDATION_ORG_SLUG = "validation"
VALIDATION_ORG_NAME = "Validation Org"


@pytest.fixture
async def sf(tmp_path: Path):
    """Boot an isolated SQLite DB; yield its session factory."""
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'validation_org_bootstrap.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


# ===========================================================================
# ensure_validation_org — create / idempotency / no-overwrite
# ===========================================================================


class TestEnsureValidationOrg:
    @pytest.mark.anyio
    async def test_creates_validation_org(self, sf):
        row = await ensure_validation_org(sf, org_id=VALIDATION_ORG_ID, slug=VALIDATION_ORG_SLUG, name=VALIDATION_ORG_NAME)
        assert row.id == VALIDATION_ORG_ID
        assert row.slug == VALIDATION_ORG_SLUG
        assert row.name == VALIDATION_ORG_NAME
        assert row.status == "active"
        assert row.deleted_at is None

    @pytest.mark.anyio
    async def test_idempotent_does_not_duplicate(self, sf):
        await ensure_validation_org(sf, org_id=VALIDATION_ORG_ID, slug=VALIDATION_ORG_SLUG, name=VALIDATION_ORG_NAME)
        await ensure_validation_org(sf, org_id=VALIDATION_ORG_ID, slug=VALIDATION_ORG_SLUG, name=VALIDATION_ORG_NAME)

        async with sf() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(OrganizationRow).where(OrganizationRow.id == VALIDATION_ORG_ID))
        assert count == 1

    @pytest.mark.anyio
    async def test_idempotent_does_not_overwrite_existing(self, sf):
        # A deployment may have renamed the validation org; re-run must not
        # clobber it (mirrors ensure_default_org's contract).
        await ensure_validation_org(sf, org_id=VALIDATION_ORG_ID, slug=VALIDATION_ORG_SLUG, name=VALIDATION_ORG_NAME)
        await ensure_validation_org(sf, org_id=VALIDATION_ORG_ID, slug="renamed", name="Renamed")

        async with sf() as session:
            row = await session.get(OrganizationRow, VALIDATION_ORG_ID)
        assert row.slug == VALIDATION_ORG_SLUG
        assert row.name == VALIDATION_ORG_NAME

    @pytest.mark.anyio
    async def test_coexists_with_default_org(self, sf):
        # The whole point of the validation phase: two Org rows in one DB.
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug="default", name="Default Organization")
        await ensure_validation_org(sf, org_id=VALIDATION_ORG_ID, slug=VALIDATION_ORG_SLUG, name=VALIDATION_ORG_NAME)

        async with sf() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(OrganizationRow))
        assert count == 2


# ===========================================================================
# Inertness — no Membership / RoleBinding created (PR-025B scope boundary)
# ===========================================================================


class TestValidationOrgIsInert:
    @pytest.mark.anyio
    async def test_no_membership_created(self, sf):
        await ensure_validation_org(sf, org_id=VALIDATION_ORG_ID, slug=VALIDATION_ORG_SLUG, name=VALIDATION_ORG_NAME)

        async with sf() as session:
            membership_count = await session.scalar(sa.select(sa.func.count()).select_from(OrgMembershipRow).where(OrgMembershipRow.org_id == VALIDATION_ORG_ID))
        assert membership_count == 0

    @pytest.mark.anyio
    async def test_no_role_binding_created(self, sf):
        await ensure_validation_org(sf, org_id=VALIDATION_ORG_ID, slug=VALIDATION_ORG_SLUG, name=VALIDATION_ORG_NAME)

        async with sf() as session:
            binding_count = await session.scalar(sa.select(sa.func.count()).select_from(RoleBindingRow).where(RoleBindingRow.org_id == VALIDATION_ORG_ID))
        assert binding_count == 0


# ===========================================================================
# Audit events — create vs exists both observed, never silent
# ===========================================================================


class TestValidationOrgAuditEvents:
    @pytest.mark.anyio
    async def test_created_event_emitted(self, sf, caplog):
        with caplog.at_level(logging.INFO, logger="deerflow.tenancy.audit_events"):
            await ensure_validation_org(sf, org_id=VALIDATION_ORG_ID, slug=VALIDATION_ORG_SLUG, name=VALIDATION_ORG_NAME)
        assert any("validation_org_created" in rec.message for rec in caplog.records)

    @pytest.mark.anyio
    async def test_exists_event_emitted_on_rerun(self, sf, caplog):
        await ensure_validation_org(sf, org_id=VALIDATION_ORG_ID, slug=VALIDATION_ORG_SLUG, name=VALIDATION_ORG_NAME)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="deerflow.tenancy.audit_events"):
            await ensure_validation_org(sf, org_id=VALIDATION_ORG_ID, slug=VALIDATION_ORG_SLUG, name=VALIDATION_ORG_NAME)
        assert any("validation_org_exists" in rec.message for rec in caplog.records)
        assert not any("validation_org_created" in rec.message for rec in caplog.records)


# ===========================================================================
# Lifespan hook phase-gating
# ===========================================================================


class TestLifespanPhaseGate:
    """The gateway lifespan hook must only seed the validation Org when phase
    is exactly ``validation``. We exercise the hook's phase branch directly
    via the hook function so we don't need to boot the full FastAPI app.

    Note: ``app.gateway.__init__`` re-exports the FastAPI ``app`` instance, so
    ``app.gateway.app`` attribute access resolves to that instance, not the
    submodule. Grab the real module object from ``sys.modules`` to patch the
    hook's module-level ``get_app_config`` binding.
    """

    @staticmethod
    def _gateway_module():
        import sys

        # Ensure the submodule is imported (its package __init__ re-exports
        # the FastAPI instance under the same name, so attribute access on
        # the package won't yield the module).
        import app.gateway.app  # noqa: F401

        return sys.modules["app.gateway.app"]

    @pytest.mark.anyio
    async def test_hook_no_op_when_phase_disabled(self, sf, monkeypatch):
        from deerflow.config.app_config import AppConfig

        cfg = AppConfig(sandbox={"use": "LocalSandboxProvider"})
        assert cfg.tenancy.multi_org.phase == "disabled"

        gw = self._gateway_module()
        monkeypatch.setattr(gw, "get_app_config", lambda: cfg)

        from types import SimpleNamespace

        await gw._ensure_validation_org(SimpleNamespace())

        async with sf() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(OrganizationRow).where(OrganizationRow.id == VALIDATION_ORG_ID))
        assert count == 0

    @pytest.mark.anyio
    async def test_hook_seeds_org_when_phase_validation(self, sf, monkeypatch):
        from deerflow.config.app_config import AppConfig

        cfg = AppConfig(
            sandbox={"use": "LocalSandboxProvider"},
            tenancy={
                "multi_org": {
                    "phase": "validation",
                    "validation_org": {
                        "id": VALIDATION_ORG_ID,
                        "slug": VALIDATION_ORG_SLUG,
                        "name": VALIDATION_ORG_NAME,
                    },
                }
            },
        )

        gw = self._gateway_module()
        monkeypatch.setattr(gw, "get_app_config", lambda: cfg)

        from types import SimpleNamespace

        await gw._ensure_validation_org(SimpleNamespace())

        async with sf() as session:
            row = await session.get(OrganizationRow, VALIDATION_ORG_ID)
        assert row is not None
        assert row.status == "active"

    @pytest.mark.anyio
    async def test_hook_no_op_when_phase_active(self, sf, monkeypatch):
        # active also does not seed (validation Org is the validation-phase
        # action; active is the operator-flip state — see hook docstring).
        from deerflow.config.app_config import AppConfig

        cfg = AppConfig(
            sandbox={"use": "LocalSandboxProvider"},
            tenancy={
                "multi_org": {
                    "phase": "active",
                    "validation_org": {
                        "id": VALIDATION_ORG_ID,
                        "slug": VALIDATION_ORG_SLUG,
                        "name": VALIDATION_ORG_NAME,
                    },
                }
            },
        )

        gw = self._gateway_module()
        monkeypatch.setattr(gw, "get_app_config", lambda: cfg)

        from types import SimpleNamespace

        await gw._ensure_validation_org(SimpleNamespace())

        async with sf() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(OrganizationRow).where(OrganizationRow.id == VALIDATION_ORG_ID))
        assert count == 0
