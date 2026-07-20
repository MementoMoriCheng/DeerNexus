"""Helpers for router-level tests that need a stubbed auth context.

The production gateway runs ``AuthMiddleware`` (validates the JWT cookie)
ahead of every router, plus ``@require_permission(owner_check=True)``
decorators that read ``request.state.auth`` and call
``thread_store.check_access``. Router-level unit tests construct
**bare** FastAPI apps that include only one router — they have neither
the auth middleware nor a real thread_store, so the decorators raise
401 (TestClient path) or ValueError (direct-call path).

This module provides several surfaces:

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

4. PR-032 RBAC seed helpers (:func:`rbac_sf`, :func:`bootstrap_rbac`,
   :func:`seed_rbac_org` / :func:`seed_rbac_user` /
   :func:`seed_rbac_membership` / :func:`bind_rbac_role`) — a shared
   implementation of the IAM seed pattern first introduced in
   ``test_iam_authorize.py``. Every Thread/Run/Artifact/Upload/
   Feedback/Suggestion router test migrated to ``make_rbac_test_app``
   needs an org + builtin roles + user + active membership + role
   binding before ``authorize()`` will allow anything; centralising the
   helpers here keeps the seed shape identical across files and lets
   the dedicated RBAC matrix test (``test_rbac_runtime_routers.py``)
   vary just the role/status axis.

Both ``make_*`` helpers are deliberately permissive: they never deny a
request by construction. Tests that want to verify the *auth boundary*
itself (e.g. ``test_auth_middleware``, ``test_auth_type_system``) build
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

# Default seed coordinates for the RBAC IAM bootstrap helpers below.
# ``make_rbac_test_app`` and ``bootstrap_rbac`` use these as defaults so
# a plain ``make_rbac_test_app(sf=sf)`` + ``bootstrap_rbac(sf)`` pair
# lines up out of the box (stub user's id == seeded user_id). The id is
# a real UUID because ``User`` is a pydantic model that validates the
# field — ``test_iam_authorize.py`` uses ``SimpleNamespace`` stand-ins
# so it keeps its own short ``"u-test"`` literal.
RBAC_DEFAULT_ORG_ID = "org-test"
RBAC_DEFAULT_USER_ID = "00000000-0000-4000-8000-000000000001"


def _make_stub_user() -> User:
    """A deterministic test user — same shape as production, fresh UUID."""
    return User(
        email="router-test@example.com",
        password_hash="x",
        system_role="user",
        id=uuid4(),
    )


def _make_rbac_stub_user() -> User:
    """Stub user whose ``id`` matches :data:`RBAC_DEFAULT_USER_ID`.

    Counterpart of :func:`_make_stub_user` for real-authorize mode: the
    IAM seed (``bootstrap_rbac``) writes the membership / role binding
    against ``RBAC_DEFAULT_USER_ID``, so the request's user must carry
    that same id or ``authorize()`` finds no membership and denies with
    403. ``make_rbac_test_app`` picks this factory automatically in
    real-authorize mode.
    """
    return User(
        email="rbac-test@example.com",
        password_hash="x",
        system_role="user",
        id=RBAC_DEFAULT_USER_ID,
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
        bypass_authorize: bool = False,
    ) -> None:
        super().__init__(app)
        self._user_factory = user_factory
        self._org_id = org_id
        self._auth_method = auth_method
        self._bypass_authorize = bypass_authorize

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        user = self._user_factory()
        user_id = str(user.id)
        request.state.user = user
        # Keep request.state.auth populated too so any handler that still
        # reads the legacy AuthContext (e.g. shared utility helpers) keeps
        # working during the PR-032 → PR-033 transition.
        request.state.auth = AuthContext(user=user, permissions=list(_STUB_PERMISSIONS))

        if self._bypass_authorize:
            # Business-logic router tests (PR-032 migration): they exercise
            # the handler, not the RBAC boundary, so they set the same
            # bypass flag that ``_make_test_request_stub`` uses for
            # direct-call tests. ``require_rbac`` returns before touching
            # the Authorize Service, so no DB / IAM seed is needed. RBAC
            # boundary coverage lives in ``test_rbac_runtime_routers.py``.
            #
            # Stored on ``request.state`` (not the Request instance)
            # because Starlette's ``BaseHTTPMiddleware`` rebuilds the
            # Request object between dispatch and the route handler, so
            # instance attributes do not survive — only scope-backed
            # ``request.state`` does. ``require_rbac`` checks both.
            request.state._deerflow_test_bypass_auth = True  # type: ignore[attr-defined]
            return await call_next(request)

        from deerflow.contracts import (
            PrincipalRef,
            TenantContext,
            bind_tenant_context,
            reset_tenant_context,
        )

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
    org_id: str = RBAC_DEFAULT_ORG_ID,
    sf: async_sessionmaker | None = None,
    user_factory: Callable[[], User] | None = None,
    owner_check_passes: bool = True,
    auth_method: str = "session",
    bypass_authorize: bool = False,
) -> FastAPI:
    """Build a FastAPI test app wired for ``@require_rbac`` (PR-032).

    Installs :class:`_StubRbacMiddleware`. Two modes:

    * **Real-authorize mode** (``bypass_authorize=False``, ``sf`` given):
      the middleware binds a real :class:`TenantContext` for ``org_id``
      so ``require_rbac`` resolves ``get_tenant_context()`` and calls
      ``AuthorizeService.authorize()``. The AuthorizeService singleton is
      (re)initialised against ``sf`` so permission checks hit the
      test-seeded IAM rows. Use this for the RBAC boundary / matrix tests.

    * **Bypass mode** (``bypass_authorize=True``): the middleware sets
      ``request._deerflow_test_bypass_auth = True``, so ``require_rbac``
      returns before touching the Authorize Service — no DB / IAM seed
      needed. Use this for migrated business-logic router tests whose
      concern is the handler behaviour, not the permission boundary.
      Semantically equivalent to the old ``make_authed_test_app``
      (which stamped a full-permission ``AuthContext``); RBAC coverage
      is provided separately by ``test_rbac_runtime_routers.py`` and by
      PR-031's ``test_iam_authorize.py``.

    Args:
        sf: Session factory from ``get_session_factory()`` (the same one
            ``bootstrap_rbac`` seeded into). Required for real-authorize
            mode; ignored in bypass mode.
        org_id: Org id to bind on every request's tenant context.
            Must match the seeded org row (real-authorize mode only).
        user_factory: Override the default test user. In real-authorize
            mode the factory's ``User.id`` must match the seeded
            membership's ``user_id`` and the role binding's
            ``principal_id``.
        owner_check_passes: When True (default), the stub thread_store
            returns True for ``check_access``. Pass False to test the
            cross-user 404 path (real-authorize mode only; bypass mode
            skips owner_check entirely).
        auth_method: ``TenantContext.auth_method`` (real-authorize mode
            only). Use ``"session"`` (default) to exercise the
            ``authorize()`` path; pass ``"internal"`` (and pair with an
            ``X-DeerFlow-Owner-User-Id`` request header + an
            ``INTERNAL_SYSTEM_ROLE`` user) to test the white-list
            short-circuit.
        bypass_authorize: Skip ``authorize()`` + ``owner_check``
            entirely (bypass mode). See above.

    Caller responsibilities for real-authorize mode (in order):

    1. Depend on the ``rbac_sf`` fixture (or ``init_engine`` manually).
    2. ``await bootstrap_rbac(sf, role_name=ORG_ADMIN_ROLE_NAME)``.
    3. Call this factory with ``sf=sf``.
    4. ``app.include_router(<router>)``.
    5. Run ``TestClient(app)``.
    6. ``rbac_sf`` teardown (or ``close_engine`` +
       ``reset_authorize_service_for_testing()``) runs automatically.

    Bypass mode needs none of that — just ``make_rbac_test_app(bypass_authorize=True)``
    and ``app.include_router(...)``.
    """
    # Real-authorize mode needs a user whose id matches the IAM seed
    # (RBAC_DEFAULT_USER_ID); bypass mode is identity-agnostic so the
    # random-uuid stub is fine.
    factory = user_factory or (_make_rbac_stub_user if not bypass_authorize else _make_stub_user)
    app = FastAPI()
    app.add_middleware(
        _StubRbacMiddleware,
        user_factory=factory,
        org_id=org_id,
        auth_method=auth_method,
        bypass_authorize=bypass_authorize,
    )

    repo = MagicMock()
    repo.check_access = AsyncMock(return_value=owner_check_passes)
    app.state.thread_store = repo

    if bypass_authorize:
        # No DB touch — return early before rebinding the AuthorizeService.
        return app

    if sf is None:
        raise ValueError("make_rbac_test_app(bypass_authorize=False) requires sf=... (a session factory from rbac_sf / get_session_factory). Pass bypass_authorize=True for business-logic tests that don't exercise the RBAC boundary.")

    # Re-bind the AuthorizeService singleton to the test factory so
    # ``require_rbac``'s ``get_authorize_service()`` call hits the
    # seeded IAM rows. reset_ first so back-to-back tests don't leak
    # a stale factory from a previous module, then drop our instance
    # directly into the module-level cache so the lazy factory returns
    # it instead of constructing one from the (likely-mismatched)
    # ``get_session_factory()`` default.
    from app.gateway.authorize import AuthorizeService, reset_authorize_service_for_testing

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


# ---------------------------------------------------------------------------
# PR-032 — RBAC seed helpers (shared IAM fixture for require_rbac tests)
# ---------------------------------------------------------------------------
#
# ``require_rbac`` calls ``AuthorizeService.authorize()``, which JOINs
# role_bindings → roles on the DB. Any TestClient-driven router test
# therefore needs an org + the three builtin roles + a user + an active
# membership + a role binding before a single request will pass. These
# helpers centralise that seed so every migrated router test file
# (threads / thread_runs / runs / artifacts / uploads / feedback /
# suggestions) and the dedicated matrix test use an identical shape.
#
# They are the public counterpart of the private ``_bootstrap`` /
# ``_seed_*`` helpers in ``test_iam_authorize.py`` — that file keeps its
# own copies (already wired into ~50 tests) and the two implementations
# are intentionally kept in sync by convention rather than by import,
# so the IAM-authorize unit tests stay self-contained.
#
# The accompanying ``rbac_sf`` session-factory fixture lives in
# ``conftest.py`` (the standard pytest location) so test modules don't
# need to import it and trigger F811 "redefinition" warnings.


async def seed_rbac_org(
    sf,
    *,
    org_id: str = RBAC_DEFAULT_ORG_ID,
    status: str = "active",
) -> None:
    """Insert one ``OrganizationRow`` (FK target for memberships)."""
    import deerflow.persistence.models  # noqa: F401 — register ORM
    from deerflow.persistence.orgs.model import OrganizationRow

    async with sf() as session:
        session.add(OrganizationRow(id=org_id, slug=org_id, name=org_id, status=status))
        await session.commit()


async def seed_rbac_user(
    sf,
    *,
    user_id: str = RBAC_DEFAULT_USER_ID,
    system_role: str = "user",
):
    """Insert one ``UserRow`` and return it (idempotent on ``user_id``)."""
    import deerflow.persistence.models  # noqa: F401 — register ORM
    from deerflow.persistence.user.model import UserRow

    async with sf() as session:
        if (existing := await session.get(UserRow, user_id)) is not None:
            return existing
        user = UserRow(id=user_id, email=f"{user_id}@example.com", system_role=system_role)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def seed_rbac_membership(
    sf,
    *,
    org_id: str = RBAC_DEFAULT_ORG_ID,
    user_id: str = RBAC_DEFAULT_USER_ID,
    status: str = "active",
) -> None:
    """Insert one ``OrgMembershipRow`` (seeds the user first if needed)."""
    import deerflow.persistence.models  # noqa: F401 — register ORM
    from deerflow.persistence.orgs.model import OrgMembershipRow

    await seed_rbac_user(sf, user_id=user_id)
    async with sf() as session:
        session.add(
            OrgMembershipRow(
                id=f"m-{org_id}-{user_id}-{status}",
                org_id=org_id,
                user_id=user_id,
                status=status,
            )
        )
        await session.commit()


async def bind_rbac_role(
    sf,
    *,
    org_id: str = RBAC_DEFAULT_ORG_ID,
    user_id: str = RBAC_DEFAULT_USER_ID,
    role_name: str,
    expires_at: datetime | None = None,
) -> None:
    """Bind ``user_id`` to the builtin ``role_name`` in ``org_id``.

    Requires :func:`seed_rbac_builtin_roles` to have run first (the
    lookup is by ``(name, is_system)``).
    """
    from sqlalchemy import select

    from deerflow.persistence.iam.model import RoleBindingRow, RoleRow

    async with sf() as session:
        role = (await session.execute(select(RoleRow).where(RoleRow.name == role_name, RoleRow.is_system.is_(True)))).scalar_one()
        binding = RoleBindingRow(
            id=uuid4().hex,
            org_id=org_id,
            principal_type="user",
            principal_id=user_id,
            role_id=role.id,
            expires_at=expires_at,
        )
        session.add(binding)
        await session.commit()


async def seed_rbac_builtin_roles(sf) -> None:
    """Idempotently seed the three builtin Org roles (org:admin/developer/viewer)."""
    from deerflow.tenancy import ensure_builtin_roles

    await ensure_builtin_roles(sf)


async def bootstrap_rbac(
    sf,
    *,
    org_id: str = RBAC_DEFAULT_ORG_ID,
    user_id: str = RBAC_DEFAULT_USER_ID,
    system_role: str = "user",
    membership_status: str | None = "active",
    org_status: str = "active",
    role_name: str | None = None,
) -> None:
    """One-shot IAM seed: org + builtin roles + user + membership + binding.

    Mirrors ``test_iam_authorize._bootstrap`` exactly. Pass
    ``membership_status=None`` to skip the membership (drives the
    no-membership → 403 matrix cell), or ``role_name=None`` to bind no
    role (drives the no-binding cell).
    """
    await seed_rbac_org(sf, org_id=org_id, status=org_status)
    await seed_rbac_builtin_roles(sf)
    await seed_rbac_user(sf, user_id=user_id, system_role=system_role)
    if membership_status is not None:
        await seed_rbac_membership(sf, org_id=org_id, user_id=user_id, status=membership_status)
    if role_name is not None:
        await bind_rbac_role(sf, org_id=org_id, user_id=user_id, role_name=role_name)
