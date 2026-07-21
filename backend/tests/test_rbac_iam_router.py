"""RBAC matrix tests for the IAM-domain ``require_rbac`` gates (PR-034).

Track C 第五刀的验收测试。PR-034 把 IAM router 的 10 个端点全部
gate 在 ``Permission.ADMIN_IAM_READ`` (读) / ``Permission.ADMIN_IAM_MANAGE``
(写)上 —— 两者都只在 ``org:admin`` 角色里 (PR-030 registry pin)。

本文件用真路径 (``make_rbac_test_app(sf=sf)`` + ``bootstrap_rbac``)
验证 ``require_rbac`` 在 admin:iam:* 权限域的行为:

* §9.1 角色矩阵 —— ``org:admin`` / ``org:developer`` / ``org:viewer``
  × 两个 IAM 能力。Oracle = ``BUILTIN_ROLE_PERMISSIONS``。
  admin 全允许; developer/viewer 全拒 (admin:iam:* 是 admin-only)。
* §9.2 状态映射 —— 无 membership / suspended membership /
  suspended org / 无 binding 全部 → 403。用 ``ADMIN_IAM_READ`` 作为
  最 permissive 的 IAM 读能力,这样拒绝归因于状态而非角色。
* 观测 —— ``policy.evaluated`` 在 allow/deny 各发一次,level 分别为
  INFO/WARNING。

为了把"装饰器层的 RBAC 决策"与"handler 业务逻辑"完全隔离,本文件挂
的是最小 dummy router (每个能力一个端点,直接返回 200),而不是真实
的 IAM router —— 这样 403 来自 ``authorize()`` 而非 handler 副作用
(例如 404 SA 不存在),矩阵语义干净。IAM router 的业务路径覆盖见
``test_iam_router_business.py``。

标记:``@pytest.mark.anyio`` + ``@pytest.mark.parametrize``,docstring
引 ``IAM-21x`` (承接 ``test_rbac_admin_routers.py`` 的 ``IAM-2xx``)。
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
# Capabilities under test (§9.1 IAM subset)
# ---------------------------------------------------------------------------
#
# Both IAM permissions cover the 10 endpoints in PR-034:
#
# * ``ADMIN_IAM_READ`` — the 3 GET endpoints (list / get / list bindings).
# * ``ADMIN_IAM_MANAGE`` — the 7 write endpoints (create / patch / disable
#   / enable / delete / create binding / delete binding).
#
# developer / viewer deny both. The matrix is self-checking against
# ``BUILTIN_ROLE_PERMISSIONS`` (the PR-030 pin).

_CAPABILITIES: list[tuple[str, Permission]] = [
    ("admin:iam:read", Permission.ADMIN_IAM_READ),
    ("admin:iam:manage", Permission.ADMIN_IAM_MANAGE),
]


def _expect_allows(role_name: str, permission: Permission) -> bool:
    """Oracle: does ``role_name`` grant ``permission`` per the registry?"""
    return permission in BUILTIN_ROLE_PERMISSIONS[role_name]


# ===========================================================================
# IAM-210 — §9.1 role matrix (allow/deny per builtin role, IAM domain)
# ===========================================================================


class TestRoleMatrix:
    """§9.1: each builtin role × each IAM capability → 200 or 403.

    Oracle is ``BUILTIN_ROLE_PERMISSIONS`` (PR-030), so the matrix is
    self-checking: a mismatch between the decorator and the registry
    fails the test. ``org:admin`` allows both IAM capabilities;
    developer / viewer deny both (admin:iam:* is admin-only per ADR §4).
    """

    @pytest.mark.parametrize("role_name", [ORG_ADMIN_ROLE_NAME, ORG_DEVELOPER_ROLE_NAME, ORG_VIEWER_ROLE_NAME])
    @pytest.mark.parametrize("cap_name, permission", _CAPABILITIES)
    @pytest.mark.anyio
    async def test_role_capability_cell(self, rbac_sf, role_name: str, cap_name: str, permission: Permission):
        """IAM-210: role ``role_name`` × capability ``cap_name`` matches the registry."""
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
# IAM-211 — §9.2 denial state mapping (IAM domain)
# ===========================================================================


class TestDenialStates:
    """§9.2: membership / org states that must produce 403 (IAM domain).

    All cells use ``ADMIN_IAM_READ`` (the most permissive IAM capability)
    so the denial is attributable to the state under test, not to a
    missing role grant. ``org:admin`` is bound where applicable to
    isolate the state variable from the role variable.
    """

    @pytest.mark.anyio
    async def test_no_membership_denies_403(self, rbac_sf):
        """IAM-211a: user exists + org exists but no membership row → 403."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME, membership_status=None)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_IAM_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.ADMIN_IAM_READ])
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_suspended_membership_denies_403(self, rbac_sf):
        """IAM-211b: membership status == suspended → 403 (active_principal fails)."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME, membership_status="suspended")

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_IAM_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.ADMIN_IAM_READ])
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_suspended_org_denies_403(self, rbac_sf):
        """IAM-211c: org status == suspended → 403 (organization_state fails)."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME, org_status="suspended")

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_IAM_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.ADMIN_IAM_READ])
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_no_role_binding_denies_403(self, rbac_sf):
        """IAM-211d: active membership but no role binding → 403 (empty permission set)."""
        await bootstrap_rbac(rbac_sf, role_name=None)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_IAM_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.ADMIN_IAM_READ])
        assert response.status_code == 403


# ===========================================================================
# IAM-212 — policy.evaluated observation (IAM domain)
# ===========================================================================


class TestObservation:
    """``policy.evaluated`` fires once per decision (observability §3.4).

    allow → INFO / outcome="allowed"; deny → WARNING /
    outcome="denied". Mirrors the runtime / admin coverage but against
    an IAM capability.
    """

    @pytest.mark.anyio
    async def test_allow_emits_info_event(self, rbac_sf):
        """IAM-212a: allowed IAM request emits one policy.evaluated at INFO."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_IAM_READ))

        with patch("app.gateway.rbac.emit_event") as mock_emit:
            with TestClient(app) as client:
                response = client.get(_PROBE_PATH[Permission.ADMIN_IAM_READ])

        assert response.status_code == 200
        policy_calls = [c for c in mock_emit.call_args_list if c.args and c.args[0] == "policy.evaluated"]
        assert len(policy_calls) == 1
        kwargs = policy_calls[0].kwargs
        assert kwargs["level"] == logging.INFO
        assert kwargs["outcome"] == "allowed"
        assert kwargs["permission"] == Permission.ADMIN_IAM_READ.value
        assert kwargs["error_code"] is None
        assert kwargs["org_id"] == RBAC_DEFAULT_ORG_ID
        assert kwargs["principal_id"] == RBAC_DEFAULT_USER_ID

    @pytest.mark.anyio
    async def test_deny_emits_warning_event(self, rbac_sf):
        """IAM-212b: denied IAM request emits one policy.evaluated at WARNING."""
        # viewer denies ADMIN_IAM_MANAGE
        await bootstrap_rbac(rbac_sf, role_name=ORG_VIEWER_ROLE_NAME)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.ADMIN_IAM_MANAGE))

        with patch("app.gateway.rbac.emit_event") as mock_emit:
            with TestClient(app) as client:
                response = client.get(_PROBE_PATH[Permission.ADMIN_IAM_MANAGE])

        assert response.status_code == 403
        policy_calls = [c for c in mock_emit.call_args_list if c.args and c.args[0] == "policy.evaluated"]
        assert len(policy_calls) == 1
        kwargs = policy_calls[0].kwargs
        assert kwargs["level"] == logging.WARNING
        assert kwargs["outcome"] == "denied"
        assert kwargs["permission"] == Permission.ADMIN_IAM_MANAGE.value
        assert kwargs["error_code"] is not None  # PERMISSION_DENIED
        assert kwargs["org_id"] == RBAC_DEFAULT_ORG_ID


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _probe_router_for(permission: Permission):
    """Build a one-route probe router for ``permission``.

    Mirrors ``test_rbac_admin_routers._probe_router_for``: a trivial
    handler returning ``{"ok": True}`` so the only variable under test
    is the decorator's RBAC decision.
    """
    router = APIRouter(prefix="/probe")

    @require_rbac(permission)
    async def _probe(request: Request) -> dict:  # noqa: ARG001 — trivial handler
        return {"ok": True}

    router.add_api_route(f"/{permission.name}", _probe, methods=["GET"])
    return router


_PROBE_PATH: dict[Permission, str] = {p: f"/probe/{p.name}" for p in Permission}
