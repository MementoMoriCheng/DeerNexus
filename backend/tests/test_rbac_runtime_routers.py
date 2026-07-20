"""RBAC matrix tests for ``require_rbac`` (PR-032, §9.1 + §9.2 + observation).

Track C 第三刀的验收测试。``require_rbac`` 把 Thread/Run/Artifact/
Upload/Feedback/Suggestion 七个 runtime router 从 ``_ALL_PERMISSIONS``
flat-grant stub 切到 DB-backed ``AuthorizeService.authorize()``;本
文件用真路径(``make_rbac_test_app(sf=sf)`` + ``bootstrap_rbac``)
验证那条 authorize 链在以下维度的行为:

* §9.1 角色矩阵 —— ``org:admin`` / ``org:developer`` / ``org:viewer``
  × 五种 runtime 能力。Oracle = ``BUILTIN_ROLE_PERMISSIONS``
  (PR-030 已 pin)。
* §9.2 状态映射 —— 无 membership / suspended membership /
  suspended org 全部 → 403。
* trusted-internal-caller 白名单短路 —— IM channel worker
  (``auth_method=='internal'`` + ``X-DeerFlow-Owner-User-Id``)
  跳 ``authorize()``。
* owner_check cross-user —— ``thread_store.check_access`` 否决 → 404
  (cross-Org/cross-user 的实际承担者)。
* 观测 —— ``policy.evaluated`` 事件在 allow / deny 各发一次,
  level 分别为 INFO / WARNING。

为了把"装饰器层的 RBAC 决策"与"handler 业务逻辑"完全隔离,本
文件挂的是一个最小 dummy router(每个能力一个端点,直接返回 200),
而不是真实的 threads/runs router —— 这样 403 来自 ``authorize()``
而非 thread 不存在等 handler 副作用,矩阵语义干净。

标记:``@pytest.mark.anyio`` + ``@pytest.mark.parametrize``,
docstring 引 ``IAM-2xx``(承接 PR-031 的 ``IAM-1xx``)。
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from _router_auth_helpers import (
    RBAC_DEFAULT_ORG_ID,
    RBAC_DEFAULT_USER_ID,
    bootstrap_rbac,
    make_internal_user,
    make_rbac_test_app,
    seed_rbac_org,
)
from fastapi import Request
from fastapi.testclient import TestClient

from app.gateway.internal_auth import (
    INTERNAL_OWNER_USER_ID_HEADER_NAME,
)
from app.gateway.rbac import require_rbac
from deerflow.contracts.rbac import (
    BUILTIN_ROLE_PERMISSIONS,
    ORG_ADMIN_ROLE_NAME,
    ORG_DEVELOPER_ROLE_NAME,
    ORG_VIEWER_ROLE_NAME,
    Permission,
)

# ---------------------------------------------------------------------------
# Capabilities under test (§9.1 runtime subset)
# ---------------------------------------------------------------------------
#
# Each capability pairs a ``Permission`` with a dummy route. The expected
# allow/deny per role comes straight from ``BUILTIN_ROLE_PERMISSIONS``
# (the PR-030 pin), so the matrix is self-checking: if the decorator
# disagrees with the registry, the test fails.

_CAPABILITIES: list[tuple[str, Permission]] = [
    ("thread:read", Permission.RUNTIME_THREAD_READ),
    ("thread:write", Permission.RUNTIME_THREAD_WRITE),
    ("run:create", Permission.RUNTIME_RUN_CREATE),
    ("run:read", Permission.RUNTIME_RUN_READ),
    ("run:cancel", Permission.RUNTIME_RUN_CANCEL),
]


def _expect_allows(role_name: str, permission: Permission) -> bool:
    """Oracle: does ``role_name`` grant ``permission`` per the registry?"""
    return permission in BUILTIN_ROLE_PERMISSIONS[role_name]


# ===========================================================================
# IAM-201 — §9.1 role matrix (allow/deny per builtin role)
# ===========================================================================


class TestRoleMatrix:
    """§9.1: each builtin role × each runtime capability → 200 or 403.

    Oracle is ``BUILTIN_ROLE_PERMISSIONS`` (PR-030), so the matrix is
    self-checking: a mismatch between the decorator and the registry
    fails the test. viewer denies write/create/cancel; admin/developer
    allow everything in the runtime domain.
    """

    @pytest.mark.parametrize("role_name", [ORG_ADMIN_ROLE_NAME, ORG_DEVELOPER_ROLE_NAME, ORG_VIEWER_ROLE_NAME])
    @pytest.mark.parametrize("cap_name, permission", _CAPABILITIES)
    @pytest.mark.anyio
    async def test_role_capability_cell(self, rbac_sf, role_name: str, cap_name: str, permission: Permission):
        """IAM-201: role ``role_name`` × capability ``cap_name`` matches the registry."""
        await bootstrap_rbac(rbac_sf, role_name=role_name)

        # Build the probe app through make_rbac_test_app so the
        # _StubRbacMiddleware + AuthorizeService singleton are wired,
        # then attach a one-route dummy router for this capability.
        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(permission))

        expected_allow = _expect_allows(role_name, permission)
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[permission])

        if expected_allow:
            assert response.status_code == 200, (
                f"{role_name} should allow {cap_name} ({permission.value}) but got {response.status_code}: {response.text}"
            )
        else:
            assert response.status_code == 403, (
                f"{role_name} should deny {cap_name} ({permission.value}) but got {response.status_code}: {response.text}"
            )


# ===========================================================================
# IAM-202 — §9.2 denial state mapping
# ===========================================================================


class TestDenialStates:
    """§9.2: membership / org states that must produce 403.

    All cells use ``RUNTIME_THREAD_READ`` (the most permissive runtime
    capability) so the denial is attributable to the state under test,
    not to a missing role grant. ``org:admin`` is bound where applicable
    to isolate the state variable from the role variable.
    """

    @pytest.mark.anyio
    async def test_no_membership_denies_403(self, rbac_sf):
        """IAM-202a: user exists + org exists but no membership row → 403."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME, membership_status=None)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.RUNTIME_THREAD_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.RUNTIME_THREAD_READ])
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_suspended_membership_denies_403(self, rbac_sf):
        """IAM-202b: membership status == suspended → 403 (active_principal fails)."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME, membership_status="suspended")

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.RUNTIME_THREAD_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.RUNTIME_THREAD_READ])
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_suspended_org_denies_403(self, rbac_sf):
        """IAM-202c: org status == suspended → 403 (organization_state fails)."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME, org_status="suspended")

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.RUNTIME_THREAD_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.RUNTIME_THREAD_READ])
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_no_role_binding_denies_403(self, rbac_sf):
        """IAM-202d: active membership but no role binding → 403 (empty permission set)."""
        await bootstrap_rbac(rbac_sf, role_name=None)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.RUNTIME_THREAD_READ))
        with TestClient(app) as client:
            response = client.get(_PROBE_PATH[Permission.RUNTIME_THREAD_READ])
        assert response.status_code == 403


# ===========================================================================
# IAM-203 — trusted-internal-caller white-list short-circuit
# ===========================================================================


class TestInternalCallerShortCircuit:
    """IM channel worker path: ``auth_method=='internal'`` + owner header.

    Single-Org bootstrap doesn't seed IAM rows for connection owners, so
    ``authorize()`` would deny every channel-triggered call. The
    decorator short-circuits that combination, skipping ``authorize()``
    but still enforcing ``thread_store.check_access`` against the header
    owner. The 404 cross-user case is covered by :class:`TestOwnerCheck`.
    """

    @pytest.mark.anyio
    async def test_internal_caller_with_owner_header_is_allowed(self, rbac_sf):
        """IAM-203a: internal caller + X-DeerFlow-Owner-User-Id → 200 (short-circuit)."""
        # No IAM seed at all — the whole point is authorize() is skipped.
        await seed_rbac_org(rbac_sf)

        app = make_rbac_test_app(
            sf=rbac_sf,
            auth_method="internal",
            user_factory=lambda: make_internal_user(user_id=RBAC_DEFAULT_USER_ID),
        )
        app.include_router(_probe_router_for(Permission.RUNTIME_THREAD_READ))
        with TestClient(app) as client:
            response = client.get(
                _PROBE_PATH[Permission.RUNTIME_THREAD_READ],
                headers={INTERNAL_OWNER_USER_ID_HEADER_NAME: RBAC_DEFAULT_USER_ID},
            )
        assert response.status_code == 200, response.text


# ===========================================================================
# IAM-204 — owner_check cross-user → 404
# ===========================================================================


class TestOwnerCheck:
    """``owner_check=True`` + ``thread_store.check_access`` deny → 404.

    The cross-Org / cross-user → 404 distinction is the router layer's
    responsibility (``authorize()``'s ``resource_ref`` is a no-op in MVP
    per ADR §17); ``require_rbac`` carries it via ``check_access``.
    """

    @pytest.mark.anyio
    async def test_owner_check_deny_returns_404(self, rbac_sf):
        """IAM-204: authorize passes (admin) but check_access denies → 404."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME)

        app = make_rbac_test_app(sf=rbac_sf, owner_check_passes=False)
        from fastapi import APIRouter

        probe_router = APIRouter(prefix="/probe")

        @require_rbac(Permission.RUNTIME_THREAD_READ, owner_check=True)
        async def _probe(thread_id: str, request: Request) -> dict:  # noqa: ARG001
            return {"ok": True}

        probe_router.add_api_route("/owned/{thread_id}", _probe, methods=["GET"])
        app.include_router(probe_router)

        with TestClient(app) as client:
            response = client.get("/probe/owned/thread-1")
        assert response.status_code == 404


# ===========================================================================
# IAM-205 — policy.evaluated observation
# ===========================================================================


class TestObservation:
    """``policy.evaluated`` fires once per decision (observability §3.4).

    allow → INFO / outcome="allowed"; deny → WARNING /
    outcome="denied". Both carry permission / org_id / principal_id /
    auth_method. Not wired to AuditEvent (ADR §13 audit dir is IAM
    mutations only).
    """

    @pytest.mark.anyio
    async def test_allow_emits_info_event(self, rbac_sf):
        """IAM-205a: allowed request emits one policy.evaluated at INFO."""
        await bootstrap_rbac(rbac_sf, role_name=ORG_ADMIN_ROLE_NAME)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.RUNTIME_THREAD_READ))

        with patch("app.gateway.rbac.emit_event") as mock_emit:
            with TestClient(app) as client:
                response = client.get(_PROBE_PATH[Permission.RUNTIME_THREAD_READ])

        assert response.status_code == 200
        policy_calls = [c for c in mock_emit.call_args_list if c.args and c.args[0] == "policy.evaluated"]
        assert len(policy_calls) == 1
        kwargs = policy_calls[0].kwargs
        assert kwargs["level"] == logging.INFO
        assert kwargs["outcome"] == "allowed"
        assert kwargs["permission"] == Permission.RUNTIME_THREAD_READ.value
        assert kwargs["error_code"] is None
        assert kwargs["org_id"] == RBAC_DEFAULT_ORG_ID
        assert kwargs["principal_id"] == RBAC_DEFAULT_USER_ID

    @pytest.mark.anyio
    async def test_deny_emits_warning_event(self, rbac_sf):
        """IAM-205b: denied request emits one policy.evaluated at WARNING with the error code."""
        # viewer denies RUNTIME_THREAD_WRITE
        await bootstrap_rbac(rbac_sf, role_name=ORG_VIEWER_ROLE_NAME)

        app = make_rbac_test_app(sf=rbac_sf)
        app.include_router(_probe_router_for(Permission.RUNTIME_THREAD_WRITE))

        with patch("app.gateway.rbac.emit_event") as mock_emit:
            with TestClient(app) as client:
                response = client.get(_PROBE_PATH[Permission.RUNTIME_THREAD_WRITE])

        assert response.status_code == 403
        policy_calls = [c for c in mock_emit.call_args_list if c.args and c.args[0] == "policy.evaluated"]
        assert len(policy_calls) == 1
        kwargs = policy_calls[0].kwargs
        assert kwargs["level"] == logging.WARNING
        assert kwargs["outcome"] == "denied"
        assert kwargs["permission"] == Permission.RUNTIME_THREAD_WRITE.value
        assert kwargs["error_code"] is not None  # PERMISSION_DENIED
        assert kwargs["org_id"] == RBAC_DEFAULT_ORG_ID


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _probe_router_for(permission: Permission, *, owner_check: bool = False):
    """Build a one-route probe router for ``permission``.

    The route always returns ``{"ok": True}`` so the only variable under
    test is the decorator's RBAC decision. The URL path uses the
    ``Permission`` enum *member name* (e.g. ``RUNTIME_THREAD_READ``)
    rather than its value (``runtime:thread:read``) because the colon
    in the value is not a safe path segment for Starlette's router.
    """
    from fastapi import APIRouter

    router = APIRouter(prefix="/probe")

    @require_rbac(permission, owner_check=owner_check)
    async def _probe(thread_id: str, request: Request) -> dict:  # noqa: ARG001 — trivial handler
        return {"ok": True}

    router.add_api_route(f"/{permission.name}/{{thread_id}}", _probe, methods=["GET"])
    return router


_PROBE_PATH: dict[Permission, str] = {p: f"/probe/{p.name}/thread-1" for p in Permission}
