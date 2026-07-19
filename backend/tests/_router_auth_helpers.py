"""Helpers for router-level tests that need a stubbed auth context.

The production gateway runs ``AuthMiddleware`` (validates the JWT cookie)
ahead of every router, plus ``@require_permission(owner_check=True)``
decorators that read ``request.state.auth`` and call
``thread_store.check_access``. Router-level unit tests construct
**bare** FastAPI apps that include only one router — they have neither
the auth middleware nor a real thread_store, so the decorators raise
401 (TestClient path) or ValueError (direct-call path).

This module provides two surfaces:

1. :func:`make_authed_test_app` — wraps ``FastAPI()`` with a tiny
   ``BaseHTTPMiddleware`` that stamps a fake user / AuthContext on every
   request, plus a permissive ``thread_store`` mock on
   ``app.state``. Use from TestClient-based router tests that target
   routers still gated by the legacy ``@require_permission`` stub
   (Admin/Studio/etc., PR-033 scope).

2. :func:`call_unwrapped` — invokes the underlying function bypassing
   the ``@require_permission`` / ``@require_rbac`` decorator chain by
   walking ``__wrapped__``. Use from direct-call tests that previously
   imported the route function and called it positionally.

3. :func:`make_rbac_test_app` (PR-032) — counterpart for routers gated
   by the new ``@require_rbac`` decorator (Thread/Run/Artifact). It
   installs middleware that stamps the user **and** binds a real
   :class:`TenantContext`, then seeds an IAM org/membership/role via
   the provided session factory so ``AuthorizeService.authorize()``
   succeeds. Caller is responsible for ``init_engine`` /
   ``close_engine`` around the test.

Both ``make_*`` helpers are deliberately permissive: they never deny a
request by construction. Tests that want to verify the *auth boundary
itself* (e.g. ``test_auth_middleware``, ``test_auth_type_system``) build
their own apps with the real middleware — those should not use this
module.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.gateway.auth.models import User
from app.gateway.authz import AuthContext, Permissions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

# Default permission set granted to the stub user. Mirrors `_ALL_PERMISSIONS`
# in authz.py — kept inline so the tests don't import a private symbol.
_STUB_PERMISSIONS: list[str] = [
    Permissions.THREADS_READ,
    Permissions.THREADS_WRITE,
    Permissions.THREADS_DELETE,
    Permissions.RUNS_CREATE,
    Permissions.RUNS_READ,
    Permissions.RUNS_CANCEL,
]


def _make_stub_user() -> User:
    """A deterministic test user — same shape as production, fresh UUID."""
    return User(
        email="router-test@example.com",
        password_hash="x",
        system_role="user",
        id=uuid4(),
    )


class _StubAuthMiddleware(BaseHTTPMiddleware):
    """Stamp a fake user / AuthContext onto every request.

    Mirrors what production ``AuthMiddleware`` does after the JWT decode
    + DB lookup short-circuit, so ``@require_permission`` finds an
    authenticated context and skips its own re-authentication path.
    """

    def __init__(self, app: ASGIApp, user_factory: Callable[[], User]) -> None:
        super().__init__(app)
        self._user_factory = user_factory

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        user = self._user_factory()
        request.state.user = user
        request.state.auth = AuthContext(user=user, permissions=list(_STUB_PERMISSIONS))
        return await call_next(request)


class _StubRbacMiddleware(BaseHTTPMiddleware):
    """Stub user + bind a real ``TenantContext`` on every request (PR-032).

    For ``@require_rbac``-gated routers: ``AuthorizeService.authorize()``
    reads the contextvar-bound tenant via ``get_tenant_context()`` and
    the User off ``request.state.user``. This middleware mirrors what
    production ``AuthMiddleware`` + ``TenantResolutionMiddleware`` stamp
    together, scoped to the test's seed org.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        user_factory: Callable[[], User],
        org_id: str,
        auth_method: str = "session",
    ) -> None:
        super().__init__(app)
        self._user_factory = user_factory
        self._org_id = org_id
        self._auth_method = auth_method

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        from deerflow.contracts import (
            PrincipalRef,
            TenantContext,
            bind_tenant_context,
            reset_tenant_context,
        )

        user = self._user_factory()
        user_id = str(user.id)
        request.state.user = user
        # Keep request.state.auth populated too so any handler that still
        # reads the legacy AuthContext (e.g. shared utility helpers) keeps
        # working during the PR-032 → PR-033 transition.
        request.state.auth = AuthContext(user=user, permissions=list(_STUB_PERMISSIONS))
        tenant = TenantContext(
            org_id=self._org_id,
            principal=PrincipalRef(id=user_id, type="user", user_id=user_id),
            auth_method=self._auth_method,  # type: ignore[arg-type]
            request_id=f"rbac-test-{uuid4().hex}",
            issued_at=datetime.now(UTC),
        )
        token = bind_tenant_context(tenant)
        try:
            return await call_next(request)
        finally:
            reset_tenant_context(token)


def make_authed_test_app(
    *,
    user_factory: Callable[[], User] | None = None,
    owner_check_passes: bool = True,
) -> FastAPI:
    """Build a FastAPI test app with stub auth + permissive thread_store.

    Args:
        user_factory: Override the default test user. Must return a fully
            populated :class:`User`. Useful for cross-user isolation tests
            that need a stable id across requests.
        owner_check_passes: When True (default), ``thread_store.check_access``
            returns True for every call so ``@require_permission(owner_check=True)``
            never blocks the route under test. Pass False to verify that
            permission failures surface correctly.

    Returns:
        A ``FastAPI`` app with the stub middleware installed and
        ``app.state.thread_store`` set to a permissive mock. The
        caller is still responsible for ``app.include_router(...)``.
    """
    factory = user_factory or _make_stub_user
    app = FastAPI()
    app.add_middleware(_StubAuthMiddleware, user_factory=factory)

    repo = MagicMock()
    repo.check_access = AsyncMock(return_value=owner_check_passes)
    app.state.thread_store = repo

    return app


def make_rbac_test_app(
    *,
    sf: async_sessionmaker,
    org_id: str,
    user_factory: Callable[[], User] | None = None,
    owner_check_passes: bool = True,
    auth_method: str = "session",
) -> FastAPI:
    """Build a FastAPI test app wired for ``@require_rbac`` (PR-032).

    Unlike :func:`make_authed_test_app`, this installs
    :class:`_StubRbacMiddleware` which binds a real
    :class:`TenantContext` for ``org_id`` so the new
    ``require_rbac`` decorator can resolve
    ``get_tenant_context()``. The AuthorizeService singleton is
    (re)initialised against ``sf`` so permission checks hit the
    test-seeded IAM rows.

    Caller responsibilities (in order):

    1. ``await init_engine("sqlite", url=..., sqlite_dir=...)``
    2. Seed org + builtin roles + user + active membership + role
       binding via the ``_bootstrap`` helper in
       ``test_iam_authorize.py`` (or a sibling).
    3. Call this factory.
    4. ``app.include_router(<router>)``.
    5. Run ``TestClient(app)``.
    6. ``await close_engine()`` + ``reset_authorize_service_for_testing()``.

    Args:
        sf: Session factory from ``get_session_factory()`` (the same
            one ``_bootstrap`` seeded into).
        org_id: Org id to bind on every request's tenant context.
            Must match the seeded org row.
        user_factory: Override the default test user. The factory's
            ``User.id`` must match the seeded membership's ``user_id``
            and the role binding's ``principal_id``.
        owner_check_passes: When True (default), the stub thread_store
            returns True for ``check_access``. Pass False to test the
            cross-user 404 path.
        auth_method: ``TenantContext.auth_method``. Use ``"session"``
            (default) to exercise the ``authorize()`` path; pass
            ``"internal"`` (and pair with an
            ``X-DeerFlow-Owner-User-Id`` request header + an
            ``INTERNAL_SYSTEM_ROLE`` user) to test the white-list
            short-circuit.
    """
    from app.gateway.authorize import AuthorizeService, reset_authorize_service_for_testing

    factory = user_factory or _make_stub_user
    app = FastAPI()
    app.add_middleware(
        _StubRbacMiddleware,
        user_factory=factory,
        org_id=org_id,
        auth_method=auth_method,
    )

    repo = MagicMock()
    repo.check_access = AsyncMock(return_value=owner_check_passes)
    app.state.thread_store = repo

    # Re-bind the AuthorizeService singleton to the test factory so
    # ``require_rbac``'s ``get_authorize_service()`` call hits the
    # seeded IAM rows. reset_ first so back-to-back tests don't leak
    # a stale factory from a previous module, then drop our instance
    # directly into the module-level cache so the lazy factory returns
    # it instead of constructing one from the (likely-mismatched)
    # ``get_session_factory()`` default.
    reset_authorize_service_for_testing()
    from app.gateway import authorize as _authorize_mod

    _authorize_mod._default_service = AuthorizeService(sf)  # type: ignore[attr-defined]

    return app


def call_unwrapped[*P, R](decorated: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Invoke the underlying function of a ``@require_permission`` / ``@require_rbac``-decorated route.

    ``functools.wraps`` sets ``__wrapped__`` on each layer; we walk all
    the way down to the original handler, bypassing every authz +
    require_auth wrapper. Use from tests that need to call route
    functions directly (without TestClient) and don't want to construct
    a fake ``Request`` just to satisfy the decorator. The ``ParamSpec``
    propagates the wrapped route's signature so call sites still get
    parameter checking despite the unwrapping.
    """
    fn: Callable = decorated
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__  # type: ignore[attr-defined]
    return fn(*args, **kwargs)


# Re-exported for tests that build their own internal-user SimpleNamespace
# the same way production ``internal_auth.get_internal_user`` does.
_INTERNAL_SYSTEM_ROLE = "internal"
_INTERNAL_OWNER_USER_ID_HEADER_NAME = "X-DeerFlow-Owner-User-Id"


def make_internal_user(*, user_id: str = "default") -> SimpleNamespace:
    """Build the synthetic internal-channel-worker user for RBAC short-circuit tests.

    Mirrors ``internal_auth.get_internal_user``'s shape so
    ``@require_rbac``'s ``_is_internal_owner_request`` branch recognizes
    the request as a trusted internal caller carrying the owner header.
    """
    return SimpleNamespace(id=user_id, system_role=_INTERNAL_SYSTEM_ROLE)
