"""Last-admin protection policy tests (PR-036, ADR-0003 §7).

Exercises :func:`deerflow.tenancy.oidc_group_mapping.assert_not_last_admin`
— the policy primitive that refuses to remove the last ``org:admin``
binding in an Org. This is the §15 acceptance checkbox "最后 org:admin
保护通过".

ADR §7 (verbatim):
  "最后一个 ``org:admin`` 不得通过普通请求被删除、暂停或解绑；
   紧急移除最后管理员需 system-admin 专用流程和双人审批记录。"

The primitive is role-generic (``role_id`` parameterised) so a future
last-{role} protection reuses it; today every caller passes ``org:admin``.

IAM IDs: ``IAM-362`` series.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.iam.model import RoleRow
from deerflow.persistence.iam.repository import create_role_binding
from deerflow.tenancy.oidc_group_mapping import (
    LastAdminError,
    apply_group_mapping,
    assert_not_last_admin,
)  # used by the additive-preserves-last-admin test

ORG_ID = "org-test"
ADMIN_ROLE_ID = "r-admin"
USER_ID = "u-1"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'last_admin.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


async def _seed_role(sf) -> None:
    async with sf() as session:
        session.add(RoleRow(id=ADMIN_ROLE_ID, org_id=ORG_ID, name="org:admin", permissions=[]))
        await session.commit()


# ===========================================================================
# IAM-362a — sole admin removal refused
# ===========================================================================


class TestSoleAdminRefused:
    @pytest.mark.anyio
    async def test_sole_admin_removal_raises(self, sf):
        await _seed_role(sf)
        await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID, role_id=ADMIN_ROLE_ID)

        with pytest.raises(LastAdminError) as exc_info:
            await assert_not_last_admin(sf, org_id=ORG_ID, role_id=ADMIN_ROLE_ID, principal_id=USER_ID)

        assert exc_info.value.org_id == ORG_ID
        assert exc_info.value.role_id == ADMIN_ROLE_ID
        assert exc_info.value.remaining == 0

    @pytest.mark.anyio
    async def test_sole_admin_removal_emits_audit_event(self, sf):
        """ADR §13: last-admin protection triggered MUST be audited."""
        await _seed_role(sf)
        await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID, role_id=ADMIN_ROLE_ID)

        with patch("deerflow.tenancy.oidc_group_mapping.emit_tenant_event") as mock_emit:
            with pytest.raises(LastAdminError):
                await assert_not_last_admin(sf, org_id=ORG_ID, role_id=ADMIN_ROLE_ID, principal_id=USER_ID)

        event_types = [c.args[0] for c in mock_emit.call_args_list]
        assert "last_admin_protection_triggered" in event_types
        # The event must carry the org + principal so the audit is actionable.
        trigger_call = next(c for c in mock_emit.call_args_list if c.args[0] == "last_admin_protection_triggered")
        assert trigger_call.kwargs["org_id"] == ORG_ID
        assert trigger_call.kwargs["principal_id"] == USER_ID


# ===========================================================================
# IAM-362b — non-sole admin removal permitted
# ===========================================================================


class TestNonSolePermitted:
    @pytest.mark.anyio
    async def test_two_admins_removal_permitted(self, sf):
        await _seed_role(sf)
        await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id="u-1", role_id=ADMIN_ROLE_ID)
        await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id="u-2", role_id=ADMIN_ROLE_ID)

        # Removing u-1 leaves u-2 → no raise.
        await assert_not_last_admin(sf, org_id=ORG_ID, role_id=ADMIN_ROLE_ID, principal_id="u-1")

    @pytest.mark.anyio
    async def test_zero_admins_permits_adding_first(self, sf):
        """No existing admins: removing a phantom principal is a no-op (count stays 0).

        This is the bootstrap edge — the guard only refuses when removal
        would zero out a currently-non-zero set. A count of 0 means there
        is nothing to protect, so the call is permitted (the dedicated
        ``/initialize`` first-admin path seeds the first binding).
        """
        await _seed_role(sf)
        # No bindings at all.
        await assert_not_last_admin(sf, org_id=ORG_ID, role_id=ADMIN_ROLE_ID, principal_id="u-ghost")


# ===========================================================================
# IAM-362c — additive mapping provably preserves the last admin
# ===========================================================================


class TestAdditivePreservesLastAdmin:
    """ADR §7 + §10 rule 6: additive mapping never removes, so it cannot
    endanger the last admin. This pins that invariant end-to-end."""

    @pytest.mark.anyio
    async def test_additive_never_triggers_last_admin_protection(self, sf):
        from deerflow.persistence.iam.repository import create_oidc_group_mapping
        from deerflow.persistence.orgs.model import OrganizationRow, OrgMembershipRow
        from deerflow.persistence.user.model import UserRow

        # Seed: one sole admin (u-1) + an org + a second user with a membership.
        async with sf() as session:
            session.add(RoleRow(id=ADMIN_ROLE_ID, org_id=ORG_ID, name="org:admin", permissions=[]))
            session.add(OrganizationRow(id=ORG_ID, slug=ORG_ID, name=ORG_ID, status="active"))
            session.add(UserRow(id="u-1", email="u1@example.com", system_role="user"))
            session.add(UserRow(id="u-2", email="u2@example.com", system_role="user"))
            await session.commit()
        async with sf() as session:
            session.add(OrgMembershipRow(id="m-1", org_id=ORG_ID, user_id="u-1", status="active"))
            session.add(OrgMembershipRow(id="m-2", org_id=ORG_ID, user_id="u-2", status="active"))
            await session.commit()
        # u-1 is the sole admin.
        await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id="u-1", role_id=ADMIN_ROLE_ID)
        # A mapping rule grants admins to anyone in the "admins" group.
        await create_oidc_group_mapping(
            sf,
            issuer="https://idp.example.com",
            group_claim="groups",
            group_value="admins",
            target_org_id=ORG_ID,
            target_role_id=ADMIN_ROLE_ID,
        )

        # u-2 logs in with the admins group — additive GROWS the admin set.
        await apply_group_mapping(sf, user_id="u-2", issuer="https://idp.example.com", groups=["admins"], subject="sub-2")

        # u-1 is STILL an admin (additive never removed it).
        with patch("deerflow.tenancy.oidc_group_mapping.emit_tenant_event") as mock_emit:
            await assert_not_last_admin(sf, org_id=ORG_ID, role_id=ADMIN_ROLE_ID, principal_id="u-1")
        trigger = [c for c in mock_emit.call_args_list if c.args[0] == "last_admin_protection_triggered"]
        assert trigger == []  # no protection triggered — u-2 remains as a second admin
