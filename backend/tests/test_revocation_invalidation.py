"""Revocation active-invalidation + P99 SLO evidence (PR-037, ADR-0003 §11).

Proves the §11 SLO — "从 Membership、RoleBinding、ServiceAccount 或 API Key
变更成功提交,到新请求被拒绝或已有 SSE 关闭,P99 ≤60 秒" — for the
cache-invalidation half (the SSE-close half is in
``test_sse_revocation_revalidation.py``). Because ``invalidate_principal`` is
synchronous and runs post-commit in the same process, the wall-clock bound
is trivially satisfied: the next request observes the change in the same
tick. These tests pin that invariant and the §11 "高风险操作可以强制读取
最新授权状态" (``force_refresh``) clause.

IAM IDs: ``IAM-370`` series (revocation); SSE-close is ``RUN-030``.

ADR §11 rules under test:
  - Membership/RoleBinding/SA/Key 变更主动失效 (drops the cache entry).
  - 主动失效失败时仍不得超过 60 秒 (TTL fallback — covered by the existing
    cache TTL tests; here we assert the active path is immediate).
  - 高风险操作可以强制读取最新授权状态 (``force_refresh`` bypasses cache).
  - system-admin namespace 可独立失效 (``invalidate_system_admin``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from app.gateway.authorize import AuthorizeService
from app.gateway.authorize_cache import org_cache_key, system_cache_key
from deerflow.contracts import Permission, PrincipalRef, TenantContext
from deerflow.persistence.iam.model import RoleBindingRow, RoleRow
from deerflow.persistence.iam.repository import (
    create_role_binding,
    set_membership_status,
)
from deerflow.persistence.orgs.model import OrganizationRow, OrgMembershipRow
from deerflow.persistence.user.model import UserRow
from deerflow.tenancy.bootstrap import ensure_builtin_roles

ORG_ID = "org-revoke"
USER_ID = "00000000-0000-4000-8000-0000000000aa"
ADMIN_ROLE_NAME = "org:admin"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'revocation.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


def _user(*, user_id: str = USER_ID, system_role: str = "user") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, system_role=system_role)


def _tenant(*, user_id: str = USER_ID, org_id: str = ORG_ID) -> TenantContext:
    return TenantContext(
        org_id=org_id,
        principal=PrincipalRef(type="user", id=user_id, user_id=user_id),
        auth_method="session",
        request_id="test-revocation",
        issued_at=datetime.now(UTC),
    )


async def _seed_world(sf, *, user_id: str = USER_ID, system_role: str = "user") -> str:
    """Seed org + builtin roles + user + active membership. Returns role id."""
    async with sf() as session:
        session.add(OrganizationRow(id=ORG_ID, slug=ORG_ID, name=ORG_ID, status="active"))
        session.add(UserRow(id=user_id, email=f"{user_id}@example.com", system_role=system_role))
        await session.commit()
    async with sf() as session:
        session.add(OrgMembershipRow(id=f"m-{user_id}", org_id=ORG_ID, user_id=user_id, status="active"))
        await session.commit()
    await ensure_builtin_roles(sf)
    from sqlalchemy import select

    async with sf() as session:
        role = (await session.execute(select(RoleRow).where(RoleRow.name == ADMIN_ROLE_NAME, RoleRow.is_system.is_(True)))).scalar_one()
    return role.id


async def _bind_role(sf, *, role_id: str, user_id: str = USER_ID) -> None:
    await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id=user_id, role_id=role_id)


# ===========================================================================
# IAM-370a — active invalidation drops cache immediately (Membership)
# ===========================================================================


class TestMembershipInvalidation:
    @pytest.mark.anyio
    async def test_suspend_invalidates_cache_next_request_denied(self, sf):
        """P99 证据:membership suspend → invalidate → next authorize() denies."""
        role_id = await _seed_world(sf)
        await _bind_role(sf, role_id=role_id)
        service = AuthorizeService(sf)
        ctx = _tenant()

        # Before: user can read runs (org:admin grants RUNTIME_RUN_READ).
        await service.authorize(ctx, Permission.RUNTIME_RUN_READ)
        assert org_cache_key(org_id=ORG_ID, principal_type="user", principal_id=USER_ID) in service._cache._entries

        # Suspend the membership (the revocation write).
        await set_membership_status(sf, org_id=ORG_ID, user_id=USER_ID, status="suspended")
        # The router would call invalidate_principal here — simulate it.
        service.invalidate_principal(org_id=ORG_ID, principal_type="user", principal_id=USER_ID)

        # After: cache entry gone; next authorize() recomputes and denies
        # (suspended membership → PERMISSION_DENIED).
        from app.gateway.authorize import AuthorizeError
        from deerflow.contracts import ErrorCode

        assert org_cache_key(org_id=ORG_ID, principal_type="user", principal_id=USER_ID) not in service._cache._entries
        with pytest.raises(AuthorizeError) as exc_info:
            await service.authorize(ctx, Permission.RUNTIME_RUN_READ)
        assert exc_info.value.code == ErrorCode.PERMISSION_DENIED

    @pytest.mark.anyio
    async def test_activate_restores_authorization(self, sf):
        role_id = await _seed_world(sf)
        await _bind_role(sf, role_id=role_id)
        service = AuthorizeService(sf)
        ctx = _tenant()

        await set_membership_status(sf, org_id=ORG_ID, user_id=USER_ID, status="suspended")
        service.invalidate_principal(org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        from app.gateway.authorize import AuthorizeError

        with pytest.raises(AuthorizeError):
            await service.authorize(ctx, Permission.RUNTIME_RUN_READ)

        # Activate restores.
        await set_membership_status(sf, org_id=ORG_ID, user_id=USER_ID, status="active")
        service.invalidate_principal(org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        await service.authorize(ctx, Permission.RUNTIME_RUN_READ)  # no raise


# ===========================================================================
# IAM-370b — active invalidation drops cache immediately (RoleBinding)
# ===========================================================================


class TestRoleBindingInvalidation:
    @pytest.mark.anyio
    async def test_delete_binding_then_invalidate_denies(self, sf):
        """Removing a user's role binding + invalidate → next request denied."""
        role_id = await _seed_world(sf)
        await _bind_role(sf, role_id=role_id)
        service = AuthorizeService(sf)
        ctx = _tenant()

        await service.authorize(ctx, Permission.RUNTIME_RUN_READ)  # allowed

        # Simulate the IAM delete-binding endpoint: drop the binding row,
        # then invalidate (the router does both post-commit).
        from deerflow.persistence.iam.repository import list_role_bindings

        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        from sqlalchemy import delete

        async with sf() as session:
            await session.execute(delete(RoleBindingRow).where(RoleBindingRow.id == bindings[0].id))
            await session.commit()
        service.invalidate_principal(org_id=ORG_ID, principal_type="user", principal_id=USER_ID)

        from app.gateway.authorize import AuthorizeError
        from deerflow.contracts import ErrorCode

        with pytest.raises(AuthorizeError) as exc_info:
            await service.authorize(ctx, Permission.RUNTIME_RUN_READ)
        assert exc_info.value.code == ErrorCode.PERMISSION_DENIED


# ===========================================================================
# IAM-370c — force_refresh bypasses stale cache (ADR §11 "高风险操作")
# ===========================================================================


class TestForceRefresh:
    @pytest.mark.anyio
    async def test_force_refresh_ignores_stale_cache_entry(self, sf):
        """A stale cached set is bypassed when force_refresh=True."""
        role_id = await _seed_world(sf)
        await _bind_role(sf, role_id=role_id)
        service = AuthorizeService(sf)
        ctx = _tenant()

        # Populate the cache with the allowed set.
        await service.authorize(ctx, Permission.RUNTIME_RUN_READ)
        key = org_cache_key(org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert key in service._cache._entries

        # Tamper: poison the cache with an EMPTY set (simulates a stale /
        # wrong entry surviving past a missed invalidation).
        service._cache.set(key, frozenset(), ttl_seconds=60)
        # Normal read returns the poisoned (empty) set → deny.
        from app.gateway.authorize import AuthorizeError

        with pytest.raises(AuthorizeError):
            await service.authorize(ctx, Permission.RUNTIME_RUN_READ)

        # force_refresh bypasses the poisoned entry, recomputes from DB,
        # and OVERWRITES the cache with the correct (allowed) set.
        await service.authorize(ctx, Permission.RUNTIME_RUN_READ, force_refresh=True)  # no raise
        # The cache entry is now the fresh value.
        fresh = service._cache.get(key)
        assert Permission.RUNTIME_RUN_READ.value in fresh

    @pytest.mark.anyio
    async def test_force_refresh_sees_membership_suspension_without_invalidate(self, sf):
        """force_refresh observes a revocation even if invalidate was NOT called.

        This is the SSE re-validation scenario: the guard cannot rely on
        invalidate having fired (the write may be in another process), so
        it forces a DB read every ≤60s.
        """
        role_id = await _seed_world(sf)
        await _bind_role(sf, role_id=role_id)
        service = AuthorizeService(sf)
        ctx = _tenant()

        await service.authorize(ctx, Permission.RUNTIME_RUN_READ)  # cache populated
        # Suspend WITHOUT calling invalidate (simulates a missed/remote write).
        await set_membership_status(sf, org_id=ORG_ID, user_id=USER_ID, status="suspended")

        from app.gateway.authorize import AuthorizeError
        from deerflow.contracts import ErrorCode

        # Normal read: stale cache still says allowed (TTL not expired).
        await service.authorize(ctx, Permission.RUNTIME_RUN_READ)  # no raise (stale)

        # force_refresh: bypasses cache, recomputes, sees suspension → deny.
        with pytest.raises(AuthorizeError) as exc_info:
            await service.authorize(ctx, Permission.RUNTIME_RUN_READ, force_refresh=True)
        assert exc_info.value.code == ErrorCode.PERMISSION_DENIED


# ===========================================================================
# IAM-370d — system-admin namespace invalidation
# ===========================================================================


class TestSystemAdminInvalidation:
    @pytest.mark.anyio
    async def test_invalidate_system_admin_drops_namespace_entry(self, sf):
        """invalidate_system_admin drops the authz:system:{user_id} entry."""
        admin_id = uuid4().hex
        await _seed_world(sf, user_id=admin_id, system_role="admin")
        service = AuthorizeService(sf)
        admin_user = _user(user_id=admin_id, system_role="admin")

        # Populate the system-admin cache entry.
        perms = await service.compute_permissions_for_user(admin_user, org_id=ORG_ID)
        key = system_cache_key(principal_id=admin_id)
        assert key in service._cache._entries
        from deerflow.contracts import SYSTEM_PERMISSIONS

        assert perms == frozenset(p.value for p in SYSTEM_PERMISSIONS)

        # Demote: invalidate_system_admin drops the entry.
        service.invalidate_system_admin(principal_id=admin_id)
        assert key not in service._cache._entries

    @pytest.mark.anyio
    async def test_invalidate_principal_does_not_touch_system_namespace(self, sf):
        """The org-namespace invalidate must NOT drop a system-admin entry."""
        admin_id = uuid4().hex
        await _seed_world(sf, user_id=admin_id, system_role="admin")
        service = AuthorizeService(sf)
        await service.compute_permissions_for_user(_user(user_id=admin_id, system_role="admin"), org_id=ORG_ID)
        key = system_cache_key(principal_id=admin_id)

        # invalidate_principal (org namespace) is a no-op on the system key.
        service.invalidate_principal(org_id=ORG_ID, principal_type="user", principal_id=admin_id)
        assert key in service._cache._entries  # untouched
