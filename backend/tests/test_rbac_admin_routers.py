"""RBAC matrix tests for the admin-domain ``require_rbac`` gates (PR-033).

Track C 第四刀的验收测试。PR-033 把四个 admin-gated router
(``admin.py`` 3 个端点用 ``Permission.ADMIN_CONSOLE_READ`` /
``channels.py`` + ``channel_connections.py`` + ``mcp.py`` 共 6 个端点用
``Permission.ADMIN_ORG_MANAGE``)从 ``system_role == "admin"`` 临时
门控切到 PR-031 的 ``AuthorizeService.authorize()``。本文件用真路径
(``make_rbac_test_app(sf=sf)`` + ``bootstrap_rbac``)验证那条 authorize
链在以下维度的行为:

* §9.1 角色矩阵 —— ``org:admin`` / ``org:developer`` / ``org:viewer``
  × 两个 admin 能力。Oracle = ``BUILTIN_ROLE_PERMISSIONS``
  (PR-030 已 pin)。admin 独占 admin:*;developer/viewer 全拒。
* §9.2 状态映射 —— 无 membership / suspended membership /
  suspended org / 无 binding 全部 → 403。用 ``ADMIN_CONSOLE_READ``
  作为最 permissive 的 admin 能力,这样拒绝归因于状态而非角色。
* ``system_role == "admin"`` 不是放行源 —— 一个 ``system_role="admin"``
  但**无** ``org:admin`` binding 的用户调 admin 端点被 403。这锁定
  ADR §4.4 的语义:admin 的真实权限来源是 ``/initialize`` seed 的
  ``org:admin`` RoleBinding,不是 ``system_role`` 字段本身。
* 观测 —— ``policy.evaluated`` 在 allow/deny 各发一次,level 分别为
  INFO/WARNING。

为了把"装饰器层的 RBAC 决策"与"handler 业务逻辑"完全隔离,本
文件挂的是最小 dummy router(每个能力一个端点,直接返回 200),
而不是真实的 admin/mcp router —— 这样 403 来自 ``authorize()``
而非 handler 副作用(例如 503 store 未配置),矩阵语义干净。

标记:``@pytest.mark.anyio`` + ``@pytest.mark.parametrize``,
docstring 引 ``IAM-2xx``(承接 ``test_rbac_runtime_routers.py`` 的
``IAM-2xx``)。
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from _router_auth_helpers import (
    RBAC_DEFAULT_ORG_ID,
    RBAC_DEFAULT_USER_ID,
    bootstrap_rbac,
    make_rbac_test_app,
)
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

from app.gateway.rbac import require_rbac
from deerflow.contracts.rbac import (
    BUILTIN_ROLE_PERMISSIONS,
    ORG_ADMIN_ROLE_NAME,
    ORG_DEVELOPER_ROLE_NAME,
    ORG_VIEWER_ROLE_NAME,
    Permission,
)

# ---------------------------------------------------------------------------
# Capabilities under test (§9.1 admin subset)
# ---------------------------------------------------------------------------
#
# Two representative admin capabilities cover the two permission values
# used in PR-033:
#
# * ``ADMIN_CONSOLE_READ`` — the Org Console API (``admin.py`` 3 endpoints).
#   Carried only by ``org:admin`` (the most permissive admin capability).
# * ``ADMIN_ORG_MANAGE`` — the channels/mcp/channel_connections admin
#   operations (6 endpoints). Also carried only by ``org:admin``.
#
# developer/viewer deny both. The matrix is self-checking against
# ``BUILTIN_ROLE_PERMISSIONS`` (the PR-030 pin), so a decorator/registry
# drift fails the test.

_CAPABILITIES: list[tuple[str, Permission]] = [
    ("admin:console:read", Permission.ADMIN_CONSOLE_READ),
    ("admin:org:manage", Permission.ADMIN_ORG_MANAGE),
]


def _expect_allows(role_name: str, permission: Permission) -> bool:
    """Oracle: does ``role_name`` grant ``permission`` per the registry?"""
    return permission in BUILTIN_ROLE_PERMISSIONS[role_name]


# ===========================================================================
# IAM-206 — §9.1 role matrix (allow/deny per builtin role, admin domain)
# ===========================================================================


class TestRoleMatrix:
    """§9.1: each builtin role × each admin capability → 200 or 403.

    Oracle is ``BUILTIN_ROLE_PERMISSIONS`` (PR-030), so the matrix is
    self-checking: a mismatch between the decorator and the registry
    fails the test. ``org:admin`` allows both admin capabilities;
    developer / viewer deny both (admin:* is admin-only per ADR §4).
    """

    @pytest.mark.parametrize("role_name", [ORG_ADMIN_ROLE_NAME, ORG_DEVELOPER_ROLE_NAME, ORG_VIEWER_ROLE_NAME])
    @pytest.mark.parametrize("cap_name, permission", _CAPABILITIES)
    @pytest.mark.anyio
    async def test_role_capability_cell(self, rbac_sf, role_name: str, cap_name: str, permission: Permission):
        """IAM-206: role ``role_name`` × capability ``cap_name`` matches the registry."""
        await bootstrap_rbac(rbac_sf, role_name=role_name)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(permission))

        expected_allow = _expect_allows(role_name, permission)
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[permission])

        if expected_allow:
            assert response.status_code == 200, f"{role_name} should allow {cap_name} ({permission.value}) but got {response.status_code}: {response.text}"
        else:
            assert response.status_code == 403, f"{role_name} should deny {cap_name} ({permission.value}) but got {response.status_code}: {response.text}"


# ===========================================================================
# IAM-207 — §9.2 denial state mapping (admin domain)
# ===========================================================================


class TestDenialStates:
    """§9.2: membership / org states that must produce 403 (admin domain).

    All cells use ``ADMIN_CONSOLE_READ`` (the most permissive admin
    capability) so the denial is attributable to the state under test,
    not to a missing role grant. ``org:admin`` is bound where applicable
    to isolate the state variable from the role variable.
    """

    @pytest.mark.anyio
    async def test_no_membership_denies_403(self, rbac_sf):
        """IAM-207a: user exists + org exists but no membership row → 403."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME, membership_status=None)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_CONSOLE_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.ADMIN_CONSOLE_READ])
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_suspended_membership_denies_403(self, rbac_sf):
        """IAM-207b: membership status == suspended → 403 (active_principal fails)."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME, membership_status="suspended")

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_CONSOLE_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.ADMIN_CONSOLE_READ])
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_suspended_org_denies_403(self, rbac_sf):
        """IAM-207c: org status == suspended → 403 (organization_state fails)."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME, org_status="suspended")

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_CONSOLE_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.ADMIN_CONSOLE_READ])
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_no_role_binding_denies_403(self, rbac_sf):
        """IAM-207d: active membership but no role binding → 403 (empty permission set)."""
        await bootstrap_rbac(rbac_sf, role_name=None)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_CONSOLE_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.ADMIN_CONSOLE_READ])
        assert response.status_code == 403


# ===========================================================================
# IAM-208 — system_role="admin" is NOT a grant source without binding
# ===========================================================================


class TestSystemRoleAdminIsNotAGrant:
    """ADR §4.4: ``system_role == "admin"`` does not bypass ``authorize()``.

    The ``User.system_role`` column is a *user-shape* attribute, not a
    grant. A user with ``system_role="admin"`` but no ``org:admin``
    RoleBinding is denied admin endpoints; the same user with a binding
    is allowed. This locks the invariant that the grant source is the
    binding (seeded by ``/initialize`` / ``seed-admin-iam``), not the
    column.

    Note: ``AuthorizeService.authorize()`` builds its internal user from
    ``TenantContext.principal`` and hardcodes ``system_role="user"`` for
    the cache path (``authorize.py:290``). The short-circuit on
    ``system_role="admin"`` only fires when the User shape is plumbed
    end-to-end, which the request path does not do today — this test
    pins that current behaviour so a future change to plumb
    ``system_role`` through ``TenantContext`` is a conscious decision.
    """

    @pytest.mark.anyio
    async def test_system_admin_without_binding_is_denied_403(self, rbac_sf):
        """IAM-208a: ``system_role="admin"`` + no org:admin binding → 403."""
        # Seed the user with system_role="admin" but bind NO role.
        await bootstrap_rbac(rbac_sf, role_name=None, system_role="admin")

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_CONSOLE_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.ADMIN_CONSOLE_READ])
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_system_admin_with_org_admin_binding_is_allowed_200(self, rbac_sf):
        """IAM-208b: ``system_role="admin"`` + org:admin binding → 200.

        ``/initialize`` creates exactly this shape: a ``system_role="admin"``
        user with an active ``org:admin`` RoleBinding (seeded by
        ``_establish_admin_tenant_relationships``).
        """
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME, system_role="admin")

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_CONSOLE_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.ADMIN_CONSOLE_READ])
        assert response.status_code == 200, response.text


# ===========================================================================
# IAM-209 — policy.evaluated observation (admin domain)
# ===========================================================================


class TestObservation:
    """``policy.evaluated`` fires once per decision (observability §3.4).

    allow → INFO / outcome="allowed"; deny → WARNING /
    outcome="denied". Mirrors the runtime-domain coverage in
    ``test_rbac_runtime_routers.py`` but against an admin capability.
    """

    @pytest.mark.anyio
    async def test_allow_emits_info_event(self, rbac_sf):
        """IAM-209a: allowed admin request emits one policy.evaluated at INFO."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_CONSOLE_READ))

        with patch("app.gateway.rbac.emit_event") as mock_emit:
            with TestClient(app) as client:
                response = client.get(_PROBE_PATH[Permission.ADMIN_CONSOLE_READ])

        assert response.status_code == 200
        policy_calls = [c for c in mock_emit.call_args_list if c.args and c.args[0] == "policy.evaluated"]
        assert len(policy_calls) == 1
        kwargs = policy_calls[0].kwargs
        assert kwargs["level"] == logging.INFO
        assert kwargs["outcome"] == "allowed"
        assert kwargs["permission"] == Permission.ADMIN_CONSOLE_READ.value
        assert kwargs["error_code"] is None
        assert kwargs["org_id"] == RBAC_DEFAULT_ORG_ID
        assert kwargs["principal_id"] == RBAC_DEFAULT_USER_ID

    @pytest.mark.anyio
    async def test_deny_emits_warning_event(self, rbac_sf):
        """IAM-209b: denied admin request emits one policy.evaluated at WARNING."""
        # viewer denies ADMIN_ORG_MANAGE
        await bootstrap_rbac(rbac_sf, role_name=ORG_VIEWER_ROLE_NAME)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_ORG_MANAGE))

        with patch("app.gateway.rbac.emit_event") as mock_emit:
            with TestClient(app) as client:
                response = client.get(_PROBE_PATH[Permission.ADMIN_ORG_MANAGE])

        assert response.status_code == 403
        policy_calls = [c for c in mock_emit.call_args_list if c.args and c.args[0] == "policy.evaluated"]
        assert len(policy_calls) == 1
        kwargs = policy_calls[0].kwargs
        assert kwargs["level"] == logging.WARNING
        assert kwargs["outcome"] == "denied"
        assert kwargs["permission"] == Permission.ADMIN_ORG_MANAGE.value
        assert kwargs["error_code"] is not None  # PERMISSION_DENIED
        assert kwargs["org_id"] == RBAC_DEFAULT_ORG_ID


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _probe_router_for(permission: Permission):
    """Build a one-route probe router for ``permission``.

    The route always returns ``{"ok": True}`` so the only variable under
    test is the decorator's RBAC decision. Uses the ``Permission`` enum
    *member name* in the URL path (e.g. ``ADMIN_CONSOLE_READ``) rather
    than its value (``admin:console:read``) because the colon is not a
    safe path segment for Starlette's router.
    """
    router = APIRouter(prefix="/probe")

    @require_rbac(permission)
    async def _probe(request: Request) -> dict:  # noqa: ARG001 — trivial handler
        return {"ok": True}

    router.add_api_route(f"/{permission.name}", _probe, methods=["GET"])
    return router


_PROBE_PATH: dict[Permission, str] = {p: f"/probe/{p.name}" for p in Permission}
