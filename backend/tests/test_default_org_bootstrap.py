"""Idempotency + relationship tests for the default-Org tenant bootstrap (PR-022).

Verifies that the four seed helpers in ``deerflow.tenancy.bootstrap`` are
idempotent, create valid rows satisfying every CHECK / FK / unique
constraint, and together materialise the default-Org + initial-admin tenant
skeleton that ``pr-split-guide.md`` §7 PR-022 mandates.

Follows the conventions of ``test_tenant_schema.py`` /
``test_resource_org_schema.py``: each test boots an isolated file-backed
SQLite DB via ``init_engine`` (exercising the full bootstrap path so the
tenant/IAM tables exist) and tears it down with ``close_engine``. Parent
rows (org / user / role) are committed in a separate session before child
rows are added — the SQLite FK-at-commit hygiene.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.contracts.rbac import (
    BUILTIN_ROLE_NAMES,
    BUILTIN_ROLE_PERMISSIONS,
    BUILTIN_ROLE_TEMPLATE_VERSION,
    ORG_ADMIN_ROLE_NAME,
)
from deerflow.persistence.iam.model import RoleBindingRow, RoleRow
from deerflow.persistence.orgs.model import OrganizationRow, OrgMembershipRow
from deerflow.persistence.user.model import UserRow
from deerflow.tenancy import (
    SYSTEM_ADMIN_ROLE_NAME,
    ensure_admin_membership,
    ensure_admin_role_binding,
    ensure_builtin_roles,
    ensure_default_org,
    ensure_system_admin_role,
)

DEFAULT_ORG_ID = "default"
DEFAULT_ORG_SLUG = "default"
DEFAULT_ORG_NAME = "Default Organization"


@pytest.fixture
async def sf(tmp_path: Path):
    """Boot an isolated SQLite DB; yield its session factory."""
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'org_bootstrap.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


async def _seed_user(sf, *, user_id: str = "u-admin", email: str = "admin@example.com") -> UserRow:
    """Commit a parent user row in its own session (FK-at-commit hygiene)."""
    async with sf() as session:
        user = UserRow(id=user_id, email=email, system_role="admin")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


# ===========================================================================
# ensure_default_org
# ===========================================================================


class TestEnsureDefaultOrg:
    @pytest.mark.anyio
    async def test_creates_default_org(self, sf):
        row = await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)
        assert row.id == DEFAULT_ORG_ID
        assert row.slug == DEFAULT_ORG_SLUG
        assert row.name == DEFAULT_ORG_NAME
        assert row.status == "active"
        assert row.deleted_at is None

    @pytest.mark.anyio
    async def test_idempotent_does_not_duplicate(self, sf):
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)

        async with sf() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(OrganizationRow))
        assert count == 1

    @pytest.mark.anyio
    async def test_idempotent_does_not_overwrite_existing(self, sf):
        # A deployment may have renamed the default org; ensure_default_org
        # must not clobber it on a re-run.
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug="renamed", name="Renamed")

        async with sf() as session:
            row = await session.get(OrganizationRow, DEFAULT_ORG_ID)
        assert row.slug == DEFAULT_ORG_SLUG
        assert row.name == DEFAULT_ORG_NAME


# ===========================================================================
# ensure_system_admin_role
# ===========================================================================


class TestEnsureSystemAdminRole:
    @pytest.mark.anyio
    async def test_creates_system_template_role(self, sf):
        role = await ensure_system_admin_role(sf)
        assert role.name == SYSTEM_ADMIN_ROLE_NAME
        assert role.is_system is True
        assert role.org_id is None
        # PR-030: permissions now come from the frozen registry, not empty.
        expected = {p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_ADMIN_ROLE_NAME]}
        assert set(role.permissions) == expected
        assert role.template_version == BUILTIN_ROLE_TEMPLATE_VERSION

    @pytest.mark.anyio
    async def test_idempotent_does_not_duplicate(self, sf):
        first = await ensure_system_admin_role(sf)
        second = await ensure_system_admin_role(sf)
        assert first.id == second.id

        async with sf() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(RoleRow).where(RoleRow.name == SYSTEM_ADMIN_ROLE_NAME, RoleRow.is_system.is_(True)))
        assert count == 1

    @pytest.mark.anyio
    async def test_system_template_satisfies_null_org_check(self, sf):
        # ck_roles_system_template_allows_null_org permits org_id IS NULL only
        # when is_system = true. If the helper violated it, create+commit
        # would raise IntegrityError.
        role = await ensure_system_admin_role(sf)
        assert role.org_id is None and role.is_system is True


# ===========================================================================
# ensure_builtin_roles (PR-030)
# ===========================================================================


class TestEnsureBuiltinRoles:
    @pytest.mark.anyio
    async def test_seeds_all_three_builtin_roles(self, sf):
        roles = await ensure_builtin_roles(sf)
        assert {r.name for r in roles} == BUILTIN_ROLE_NAMES
        for role in roles:
            assert role.is_system is True
            assert role.org_id is None
            assert set(role.permissions) == {p.value for p in BUILTIN_ROLE_PERMISSIONS[role.name]}
            assert role.template_version == BUILTIN_ROLE_TEMPLATE_VERSION

    @pytest.mark.anyio
    async def test_idempotent_does_not_duplicate(self, sf):
        first = await ensure_builtin_roles(sf)
        second = await ensure_builtin_roles(sf)
        assert {r.id for r in first} == {r.id for r in second}

        async with sf() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(RoleRow).where(RoleRow.is_system.is_(True), RoleRow.name.in_(tuple(BUILTIN_ROLE_NAMES))))
        assert count == 3

    @pytest.mark.anyio
    async def test_ensure_system_admin_role_seeds_all_three(self, sf):
        # The wrapper ensure_system_admin_role now delegates to
        # ensure_builtin_roles, so a single call provisions all three roles
        # (not just org:admin). Verify the side effect so a future refactor
        # that reverts to single-role seeding is caught.
        admin = await ensure_system_admin_role(sf)
        assert admin.name == ORG_ADMIN_ROLE_NAME

        async with sf() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(RoleRow).where(RoleRow.is_system.is_(True), RoleRow.name.in_(tuple(BUILTIN_ROLE_NAMES))))
        assert count == 3


# ===========================================================================
# ensure_admin_membership
# ===========================================================================


class TestEnsureAdminMembership:
    @pytest.mark.anyio
    async def test_creates_active_membership(self, sf):
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)
        user = await _seed_user(sf)

        membership = await ensure_admin_membership(sf, org_id=DEFAULT_ORG_ID, user_id=user.id)
        assert membership.org_id == DEFAULT_ORG_ID
        assert membership.user_id == user.id
        assert membership.status == "active"

    @pytest.mark.anyio
    async def test_idempotent_does_not_duplicate(self, sf):
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)
        user = await _seed_user(sf)

        await ensure_admin_membership(sf, org_id=DEFAULT_ORG_ID, user_id=user.id)
        await ensure_admin_membership(sf, org_id=DEFAULT_ORG_ID, user_id=user.id)

        async with sf() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(OrgMembershipRow).where(OrgMembershipRow.org_id == DEFAULT_ORG_ID, OrgMembershipRow.user_id == user.id))
        assert count == 1


# ===========================================================================
# ensure_admin_role_binding
# ===========================================================================


class TestEnsureAdminRoleBinding:
    @pytest.mark.anyio
    async def test_creates_binding(self, sf):
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)
        user = await _seed_user(sf)
        role = await ensure_system_admin_role(sf)

        binding = await ensure_admin_role_binding(sf, org_id=DEFAULT_ORG_ID, user_id=user.id, role_id=role.id)
        assert binding.org_id == DEFAULT_ORG_ID
        assert binding.principal_type == "user"
        assert binding.principal_id == user.id
        assert binding.role_id == role.id

    @pytest.mark.anyio
    async def test_idempotent_does_not_duplicate(self, sf):
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)
        user = await _seed_user(sf)
        role = await ensure_system_admin_role(sf)

        await ensure_admin_role_binding(sf, org_id=DEFAULT_ORG_ID, user_id=user.id, role_id=role.id)
        await ensure_admin_role_binding(sf, org_id=DEFAULT_ORG_ID, user_id=user.id, role_id=role.id)

        async with sf() as session:
            count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(RoleBindingRow)
                .where(
                    RoleBindingRow.org_id == DEFAULT_ORG_ID,
                    RoleBindingRow.principal_id == user.id,
                    RoleBindingRow.role_id == role.id,
                )
            )
        assert count == 1


# ===========================================================================
# Full startup + /initialize sequence
# ===========================================================================


class TestFullBootstrapSequence:
    @pytest.mark.anyio
    async def test_full_sequence_then_idempotent_rerun(self, sf):
        # Phase 1 — lifespan: default Org + system admin role.
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)
        role = await ensure_system_admin_role(sf)

        # Phase 2 — /initialize: create admin user, then bind.
        user = await _seed_user(sf)
        await ensure_admin_membership(sf, org_id=DEFAULT_ORG_ID, user_id=user.id)
        await ensure_admin_role_binding(sf, org_id=DEFAULT_ORG_ID, user_id=user.id, role_id=role.id)

        # Re-run the entire sequence — row counts must be unchanged.
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)
        await ensure_system_admin_role(sf)
        await ensure_admin_membership(sf, org_id=DEFAULT_ORG_ID, user_id=user.id)
        await ensure_admin_role_binding(sf, org_id=DEFAULT_ORG_ID, user_id=user.id, role_id=role.id)

        async with sf() as session:
            org_count = await session.scalar(sa.select(sa.func.count()).select_from(OrganizationRow))
            role_count = await session.scalar(sa.select(sa.func.count()).select_from(RoleRow).where(RoleRow.is_system.is_(True)))
            membership_count = await session.scalar(sa.select(sa.func.count()).select_from(OrgMembershipRow))
            binding_count = await session.scalar(sa.select(sa.func.count()).select_from(RoleBindingRow).where(RoleBindingRow.principal_id == user.id))

        assert org_count == 1
        # PR-030: ensure_system_admin_role now delegates to ensure_builtin_roles,
        # which seeds all three builtin Org roles (org:admin/developer/viewer)
        # as system templates from the frozen registry.
        assert role_count == 3
        assert membership_count == 1
        assert binding_count == 1


# ===========================================================================
# Audit event — not silently dropped
# ===========================================================================


class TestAuditEventNoSilentDrop:
    @pytest.mark.anyio
    async def test_tenant_event_logged(self, sf, caplog):
        import logging

        from deerflow.tenancy.audit_events import emit_tenant_event

        with caplog.at_level(logging.INFO, logger="deerflow.tenancy.audit_events"):
            emit_tenant_event(
                "default_org_created",
                org_id=DEFAULT_ORG_ID,
                principal_id=None,
                payload={"slug": DEFAULT_ORG_SLUG},
            )
        assert any("default_org_created" in rec.message for rec in caplog.records)


# ===========================================================================
# Single-Org invariant (PR-025B reversibility)
# ===========================================================================


class TestSingleOrgInvariant:
    """The default-org bootstrap must stay a single-Org operation.

    PR-025B keeps the request-path resolver single-Org and adds the
    validation Org only when ``tenancy.multi_org.phase == "validation"``. The
    default bootstrap path here must therefore never create a second Org row
    on its own — that is the reversibility property (phase=disabled ==
    today's exact behaviour).
    """

    @pytest.mark.anyio
    async def test_default_bootstrap_creates_exactly_one_org(self, sf):
        await ensure_default_org(sf, org_id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)
        await ensure_system_admin_role(sf)

        async with sf() as session:
            org_count = await session.scalar(sa.select(sa.func.count()).select_from(OrganizationRow))

        assert org_count == 1
