"""Tests for /api/v1/auth/me effective_permissions + org_id surfacing (PR-057 follow-up).

The Studio UI gates write buttons client-side based on these fields; backend RBAC
remains authoritative (403 still enforced). These tests cover the three branches of
``get_me``: Org bound → sorted perms; no TenantContext → empty/None; AuthorizeError
(suspended/invited membership) → fail-closed empty perms.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.gateway.auth.models import User
from app.gateway.routers.auth import get_me
from deerflow.contracts.context import (
    PrincipalRef,
    TenantContext,
    bind_tenant_context,
    reset_tenant_context,
)


def _make_user(*, system_role: str = "user") -> User:
    return User(
        id=uuid4(),
        email="operator@example.com",
        system_role=system_role,  # type: ignore[arg-type]
    )


def _make_request() -> MagicMock:
    """A minimal Starlette Request stand-in — get_me only passes it through to deps."""
    req = MagicMock()
    req.state = MagicMock()
    return req


def _bind_tenant(org_id: str | None) -> TenantContext | None:
    if org_id is None:
        return None
    return TenantContext(
        org_id=org_id,
        principal=PrincipalRef(type="user", id="u-1"),
        auth_method="session",
        request_id="req-test",
        issued_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_me_surfaces_sorted_effective_permissions():
    """Org bound → effective_permissions is the sorted permission set, org_id echoed."""
    user = _make_user()
    ctx = _bind_tenant("org-1")
    token = bind_tenant_context(ctx)  # type: ignore[arg-type]
    try:
        authorize_svc = MagicMock()
        # Return in non-sorted order to prove the handler sorts.
        authorize_svc.compute_permissions_for_user = AsyncMock(return_value=frozenset({"studio:release:promote", "studio:package:read"}))
        with (
            patch("app.gateway.routers.auth.get_current_user_from_request", AsyncMock(return_value=user)),
            patch("app.gateway.authorize.get_authorize_service", return_value=authorize_svc),
        ):
            resp = await get_me(_make_request())
        assert resp.org_id == "org-1"
        # Sorted for deterministic client comparison / cache keys.
        assert resp.effective_permissions == ["studio:package:read", "studio:release:promote"]
        assert resp.id == str(user.id)
        assert resp.email == user.email
    finally:
        reset_tenant_context(token)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_me_admin_short_circuit_returns_all_system_perms():
    """system_role=admin path is handled inside compute_permissions_for_user; the
    handler just surfaces whatever it returns (here the full studio set)."""
    user = _make_user(system_role="admin")
    ctx = _bind_tenant("org-1")
    token = bind_tenant_context(ctx)  # type: ignore[arg-type]
    try:
        authorize_svc = MagicMock()
        authorize_svc.compute_permissions_for_user = AsyncMock(
            return_value=frozenset(
                {
                    "studio:package:read",
                    "studio:package:write",
                    "studio:release:promote_dev",
                    "studio:release:promote",
                    "studio:release:rollback",
                }
            )
        )
        with (
            patch("app.gateway.routers.auth.get_current_user_from_request", AsyncMock(return_value=user)),
            patch("app.gateway.authorize.get_authorize_service", return_value=authorize_svc),
        ):
            resp = await get_me(_make_request())
        assert len(resp.effective_permissions) == 5
        assert resp.effective_permissions == sorted(resp.effective_permissions)
    finally:
        reset_tenant_context(token)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_me_no_tenant_context_returns_empty_perms_and_none_org():
    """Pre-initialization (no TenantContext bound) → org_id=None, perms empty."""
    user = _make_user()
    # Ensure no tenant context is bound for this test.
    from deerflow.contracts.context import _current_tenant

    token = _current_tenant.set(None)
    try:
        with patch("app.gateway.routers.auth.get_current_user_from_request", AsyncMock(return_value=user)):
            resp = await get_me(_make_request())
        assert resp.org_id is None
        assert resp.effective_permissions == []
    finally:
        _current_tenant.reset(token)


@pytest.mark.asyncio
async def test_me_authorize_error_fail_closed_empty_perms():
    """Suspended/invited/removed membership → AuthorizeError → fail-closed empty perms.

    The /me call itself must NOT raise (user still sees basic profile + can be
    redirected); only the permission set degrades to empty so Studio buttons hide.
    """
    user = _make_user()
    ctx = _bind_tenant("org-1")
    token = bind_tenant_context(ctx)  # type: ignore[arg-type]
    try:
        from app.gateway.authorize import AuthorizeError
        from deerflow.contracts import ErrorCode

        authorize_svc = MagicMock()
        authorize_svc.compute_permissions_for_user = AsyncMock(side_effect=AuthorizeError(ErrorCode.PERMISSION_DENIED, "suspended"))
        with (
            patch("app.gateway.routers.auth.get_current_user_from_request", AsyncMock(return_value=user)),
            patch("app.gateway.authorize.get_authorize_service", return_value=authorize_svc),
        ):
            resp = await get_me(_make_request())
        # org_id is still surfaced (the tenant resolved), but perms are fail-closed empty.
        assert resp.org_id == "org-1"
        assert resp.effective_permissions == []
    finally:
        reset_tenant_context(token)  # type: ignore[arg-type]
