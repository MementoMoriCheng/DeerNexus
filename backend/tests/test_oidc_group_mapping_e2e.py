"""IdP E2E: OIDC group mapping → AuthorizeService grants (PR-036, ADR-0003 §10).

The PR-036 "至少选择一个 IdP 完成 E2E" deliverable. The mapping engine is
IdP-agnostic (it takes ``(issuer, groups)``), so this test injects mock
IdP claims and then drives the **real** ``AuthorizeService.compute_permissions_for_user``
to prove the materialized RoleBindings actually grant — i.e. the read-side
union picks them up with zero changes. The real OIDC code-flow / JWKS
transport (security baseline §3.1) is a separate PR; this E2E validates
the mapping→authz boundary end-to-end.

IAM IDs: ``IAM-364`` series.

Flow under test (per ADR §10):
  mock IdP claims (issuer, groups)
    → apply_group_mapping (additive)
      → RoleBinding rows materialized (created_by provenance sentinel)
        → AuthorizeService.compute_permissions_for_user
          → effective permission set reflects the mapped roles
            → a permission the user could NOT do before, they now CAN
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from app.gateway.authorize import AuthorizeService
from deerflow.contracts import (
    BUILTIN_ROLE_PERMISSIONS,
    ORG_ADMIN_ROLE_NAME,
    ORG_DEVELOPER_ROLE_NAME,
    ORG_VIEWER_ROLE_NAME,
    Permission,
)
from deerflow.persistence.iam.repository import create_oidc_group_mapping
from deerflow.persistence.orgs.model import OrganizationRow, OrgMembershipRow
from deerflow.persistence.user.model import UserRow
from deerflow.tenancy.bootstrap import ensure_builtin_roles
from deerflow.tenancy.oidc_group_mapping import apply_group_mapping

ORG_ID = "org-test"
ISSUER = "https://idp.example.com"
USER_ID = "00000000-0000-4000-8000-000000000077"
SUBJECT = "idp-sub-77"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'oidc_mapping_e2e.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


async def _bootstrap(sf) -> None:
    """Seed org + builtin roles + user + active membership (no role binding yet)."""
    async with sf() as session:
        session.add(OrganizationRow(id=ORG_ID, slug=ORG_ID, name=ORG_ID, status="active"))
        session.add(UserRow(id=USER_ID, email=f"{USER_ID}@example.com", system_role="user"))
        await session.commit()
    async with sf() as session:
        session.add(OrgMembershipRow(id="m-1", org_id=ORG_ID, user_id=USER_ID, status="active"))
        await session.commit()
    await ensure_builtin_roles(sf)


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=USER_ID, system_role="user")


async def _add_mapping_rule(sf, *, group_value: str, role_name: str) -> None:
    """Add an allowlist rule mapping ``group_value`` → the builtin ``role_name``.

    Looks up the role by (name, is_system) to get its id (the system
    templates seeded by ``ensure_builtin_roles`` are the FK target).
    """
    from sqlalchemy import select

    from deerflow.persistence.iam.model import RoleRow

    async with sf() as session:
        role = (await session.execute(select(RoleRow).where(RoleRow.name == role_name, RoleRow.is_system.is_(True)))).scalar_one()
        role_id = role.id
    await create_oidc_group_mapping(
        sf,
        issuer=ISSUER,
        group_claim="groups",
        group_value=group_value,
        target_org_id=ORG_ID,
        target_role_id=role_id,
    )


# ===========================================================================
# IAM-364a — mapped role grants via the real AuthorizeService
# ===========================================================================


class TestMappedRoleGrants:
    @pytest.mark.anyio
    async def test_viewer_group_grants_viewer_permissions(self, sf):
        """Before mapping: empty set. After mapping viewer group: viewer perms."""
        await _bootstrap(sf)
        await _add_mapping_rule(sf, group_value="viewers", role_name=ORG_VIEWER_ROLE_NAME)

        # Before: no bindings → empty effective set.
        perms_before = await AuthorizeService(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert perms_before == frozenset()

        # The IdP login hands the user the "viewers" group.
        result = await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["viewers"], subject=SUBJECT)
        assert len(result.applied) == 1

        # After: the viewer permission set is live (union picked up the new binding).
        perms_after = await AuthorizeService(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert perms_after == frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_VIEWER_ROLE_NAME])

    @pytest.mark.anyio
    async def test_specific_permission_now_allowed(self, sf):
        """The headline E2E assertion: RUNTIME_RUN_READ becomes allowed after mapping."""
        await _bootstrap(sf)
        await _add_mapping_rule(sf, group_value="viewers", role_name=ORG_VIEWER_ROLE_NAME)

        # Fresh service for the BEFORE read (its cache would otherwise mask the
        # post-mapping change — additive mapping does not invalidate the cache).
        assert Permission.RUNTIME_RUN_READ.value not in await AuthorizeService(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)

        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["viewers"], subject=SUBJECT)

        # Fresh service again so the post-mapping read is not served from the
        # BEFORE cache entry (production invalidates on binding write; this E2E
        # isolates the mapping→authz boundary, not the cache path).
        assert Permission.RUNTIME_RUN_READ.value in await AuthorizeService(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)


# ===========================================================================
# IAM-364b — union of multiple groups (ADR §10 rule 4)
# ===========================================================================


class TestUnion:
    @pytest.mark.anyio
    async def test_two_groups_union_permissions(self, sf):
        await _bootstrap(sf)
        await _add_mapping_rule(sf, group_value="viewers", role_name=ORG_VIEWER_ROLE_NAME)
        await _add_mapping_rule(sf, group_value="devs", role_name=ORG_DEVELOPER_ROLE_NAME)

        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["viewers", "devs"], subject=SUBJECT)

        perms = await AuthorizeService(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        expected = frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_VIEWER_ROLE_NAME]) | frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_DEVELOPER_ROLE_NAME])
        assert perms == expected


# ===========================================================================
# IAM-364c — allowlist filter end-to-end (rule 1: unmatched ignored)
# ===========================================================================


class TestAllowlistFilterE2e:
    @pytest.mark.anyio
    async def test_unmatched_group_grants_nothing(self, sf):
        """An IdP group with no allowlist rule maps to nothing (rule 1)."""
        await _bootstrap(sf)
        await _add_mapping_rule(sf, group_value="viewers", role_name=ORG_VIEWER_ROLE_NAME)

        # User presents a group the operator never allowlisted.
        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["super-admins"], subject=SUBJECT)

        perms = await AuthorizeService(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert perms == frozenset()

    @pytest.mark.anyio
    async def test_foreign_issuer_grants_nothing(self, sf):
        await _bootstrap(sf)
        await _add_mapping_rule(sf, group_value="viewers", role_name=ORG_VIEWER_ROLE_NAME)

        await apply_group_mapping(sf, user_id=USER_ID, issuer="https://evil.idp.attacker", groups=["viewers"], subject=SUBJECT)

        perms = await AuthorizeService(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert perms == frozenset()


# ===========================================================================
# IAM-364d — admin group cannot grant system permissions (rule 3 + §17 non-goal)
# ===========================================================================


class TestNoSystemGrant:
    @pytest.mark.anyio
    async def test_admin_group_grants_org_admin_not_system(self, sf):
        """ADR §17 non-goal: 'JIT admin' does NOT auto-grant system:* permissions.

        Mapping the admins group to ``org:admin`` grants the Org admin
        permission set (which excludes system:*) — never SYSTEM_PERMISSIONS.
        """
        from deerflow.contracts import SYSTEM_PERMISSIONS

        await _bootstrap(sf)
        await _add_mapping_rule(sf, group_value="admins", role_name=ORG_ADMIN_ROLE_NAME)

        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)

        perms = await AuthorizeService(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert perms == frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_ADMIN_ROLE_NAME])
        # No system permission leaked in (the org:admin role has none by registry).
        assert not (perms & frozenset(p.value for p in SYSTEM_PERMISSIONS))


# ===========================================================================
# IAM-364e — last-admin invariant holds under additive mapping (ADR §7)
# ===========================================================================


class TestLastAdminInvariant:
    @pytest.mark.anyio
    async def test_additive_admin_mapping_preserves_existing_admin(self, sf):
        """A pre-existing sole admin is NOT removed when a second user maps to admin.

        Additive mapping only grows the admin set (ADR §10 rule 6); it
        never endangers the last admin (ADR §7). This is the integrated
        form of ``test_last_admin_protection::TestAdditivePreservesLastAdmin``.
        """
        from deerflow.tenancy.oidc_group_mapping import assert_not_last_admin

        await _bootstrap(sf)
        await _add_mapping_rule(sf, group_value="admins", role_name=ORG_ADMIN_ROLE_NAME)
        # u-existing is the sole admin (bound directly).
        existing_admin = uuid4().hex
        async with sf() as session:
            session.add(UserRow(id=existing_admin, email=f"{existing_admin}@example.com", system_role="user"))
            await session.commit()
        from sqlalchemy import select

        from deerflow.persistence.iam.model import RoleBindingRow, RoleRow

        async with sf() as session:
            role = (await session.execute(select(RoleRow).where(RoleRow.name == ORG_ADMIN_ROLE_NAME, RoleRow.is_system.is_(True)))).scalar_one()
            role_id = role.id
        # Commit the binding in its own session so the user FK is already durable.
        async with sf() as session:
            session.add(
                RoleBindingRow(
                    id=uuid4().hex,
                    org_id=ORG_ID,
                    principal_type="user",
                    principal_id=existing_admin,
                    role_id=role_id,
                )
            )
            await session.commit()

        # A new user logs in with the admins group → additive grants them admin too.
        new_admin = uuid4().hex
        async with sf() as session:
            session.add(UserRow(id=new_admin, email=f"{new_admin}@example.com", system_role="user"))
            await session.commit()
        async with sf() as session:
            session.add(OrgMembershipRow(id=f"m-{new_admin}", org_id=ORG_ID, user_id=new_admin, status="active"))
            await session.commit()
        await apply_group_mapping(sf, user_id=new_admin, issuer=ISSUER, groups=["admins"], subject="sub-new")

        # The pre-existing admin can still be removed-or-not per last-admin:
        # removing THEM must now be PERMITTED (there is a second admin).
        await assert_not_last_admin(sf, org_id=ORG_ID, role_id=role_id, principal_id=existing_admin)
