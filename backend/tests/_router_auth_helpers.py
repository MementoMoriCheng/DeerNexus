"""Helpers for router-level tests that need a stubbed auth context.

The production gateway runs ``AuthMiddleware`` (validates the JWT cookie)
ahead of every router, then ``TenantResolutionMiddleware`` binds the
:class:`TenantContext` ContextVar. ``@require_rbac`` reads that context
and calls ``AuthorizeService.authorize()``. Router-level unit tests
construct **bare** FastAPI apps that include only one router — they
have neither the production middleware chain nor a real thread_store,
so the decorator raises 401 (TestClient path) or ValueError
(direct-call path).

This module provides several surfaces:

1. :func:`make_rbac_test_app` — wraps ``FastAPI()`` with a tiny
   ``BaseHTTPMiddleware`` that stamps a fake user and binds a real
   ``TenantContext`` on every request, plus a permissive
   ``thread_store`` mock on ``app.state``. Two modes:

   * **bypass mode** (``bypass_authorize=True``): sets the
     ``_deerflow_test_bypass_auth`` flag so ``require_rbac`` returns
     before consulting the Authorize Service. No DB / IAM seed needed.
     Use from business-logic router tests (threads / runs / admin /
     channels / mcp / …) that exercise the handler, not the permission
     boundary.
   * **real-authorize mode** (``bypass_authorize=False``, ``sf=...``):
     binds a real ``TenantContext`` and re-points the
     ``AuthorizeService`` singleton at the supplied session factory so
     ``authorize()`` consults the test-seeded IAM rows. Use from the
     RBAC boundary / matrix tests.

2. :func:`call_unwrapped` — invokes the underlying function bypassing
   the ``@require_rbac`` decorator chain by walking ``__wrapped__``.
   Use from direct-call tests that previously imported the route
   function and called it positionally.

3. PR-032 RBAC seed helpers (:func:`bootstrap_rbac`,
   :func:`seed_rbac_org` / :func:`seed_rbac_user` /
   :func:`seed_rbac_membership` / :func:`bind_rbac_role`) — a shared
   implementation of the IAM seed pattern first introduced in
   ``test_iam_authorize.py``. Every router test that runs in
   real-authorize mode needs an org + builtin roles + user + active
   membership + role binding before ``authorize()`` will allow
   anything; centralising the helpers here keeps the seed shape
   identical across files and lets the dedicated RBAC matrix tests
   (``test_rbac_runtime_routers.py``, ``test_rbac_admin_routers.py``)
   vary just the role/status axis.

The factory is deliberately permissive: it never denies a request by
construction. Tests that want to verify the *auth boundary* itself
(e.g. ``test_auth_middleware``, ``test_auth_type_system``) build their
own apps with the real middleware — those should not use this module.
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

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

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


class _StubRbacMiddleware(BaseHTTPMiddleware):
    """Stub user + bind a real ``TenantContext`` on every request.

    ``AuthorizeService.authorize()`` reads the contextvar-bound tenant
    via ``get_tenant_context()`` and the User off ``request.state.user``.
    This middleware mirrors what production ``AuthMiddleware`` +
    ``TenantResolutionMiddleware`` stamp together, scoped to the test's
    seed org.

    In bypass mode the same tenant is still bound (handlers like the
    Org Console resolve ``org_id`` from the contextvar even when the
    decorator is short-circuited), and the test-bypass flag is set so
    ``require_rbac`` returns before touching the Authorize Service.
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
        from deerflow.contracts import (
            PrincipalRef,
            TenantContext,
            bind_tenant_context,
            reset_tenant_context,
        )

        user = self._user_factory()
        user_id = str(user.id)
        request.state.user = user

        if self._bypass_authorize:
            # Business-logic router tests: they exercise the handler, not
            # the RBAC boundary, so they set the same bypass flag that
            # ``_make_test_request_stub`` uses for direct-call tests.
            # ``require_rbac`` returns before touching the Authorize
            # Service, so no DB / IAM seed is needed. RBAC boundary
            # coverage lives in ``test_rbac_*_routers.py``.
            #
            # Stored on ``request.state`` (not the Request instance)
            # because Starlette's ``BaseHTTPMiddleware`` rebuilds the
            # Request object between dispatch and the route handler, so
            # instance attributes do not survive — only scope-backed
            # ``request.state`` does. ``require_rbac`` checks both.
            #
            # No TenantContext is bound here: the autouse
            # ``_auto_user_context`` fixture (or a test-specific
            # ``_bound_tenant`` fixture) already bound one with
            # ``org_id="default"``, and ContextVar inheritance propagates
            # it into the request task. Handlers reading ``org_id`` off
            # the contextvar (Org Console's ``_require_org_id``) see that
            # value. Re-binding here would clobber the fixture's value.
            request.state._deerflow_test_bypass_auth = True  # type: ignore[attr-defined]
            return await call_next(request)

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


def make_rbac_test_app(
    *,
    org_id: str = RBAC_DEFAULT_ORG_ID,
    sf: async_sessionmaker | None = None,
    user_factory: Callable[[], User] | None = None,
    owner_check_passes: bool = True,
    auth_method: str = "session",
    bypass_authorize: bool = False,
) -> FastAPI:
    """Build a FastAPI test app wired for ``@require_rbac``.

    Installs :class:`_StubRbacMiddleware`. Two modes:

    * **Real-authorize mode** (``bypass_authorize=False``, ``sf`` given):
      the middleware binds a real :class:`TenantContext` for ``org_id``
      so ``require_rbac`` resolves ``get_tenant_context()`` and calls
      ``AuthorizeService.authorize()``. The AuthorizeService singleton is
      (re)initialised against ``sf`` so permission checks hit the
      test-seeded IAM rows. Use this for the RBAC boundary / matrix tests.

    * **Bypass mode** (``bypass_authorize=True``): the middleware sets
      ``request.state._deerflow_test_bypass_auth = True``, so
      ``require_rbac`` returns before touching the Authorize Service —
      no DB / IAM seed needed. Use this for migrated business-logic
      router tests whose concern is the handler behaviour, not the
      permission boundary. The middleware still stamps
      ``request.state.user`` and binds a default TenantContext so
      handlers that read ``org_id`` off the contextvar (e.g. the Org
      Console) keep working without a real seed.

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

    Bypass mode needs none of that — just
    ``make_rbac_test_app(bypass_authorize=True)`` and
    ``app.include_router(...)``.
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
    """Invoke the underlying function of a ``@require_rbac``-decorated route.

    ``functools.wraps`` sets ``__wrapped__`` on each layer; we walk all
    the way down to the original handler, bypassing every authz wrapper.
    Use from tests that need to call route functions directly (without
    TestClient) and don't want to construct a fake ``Request`` just to
    satisfy the decorator. The ``ParamSpec`` propagates the wrapped
    route's signature so call sites still get parameter checking
    despite the unwrapping.
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
# in real-authorize mode therefore needs an org + the three builtin
# roles + a user + an active membership + a role binding before a
# single request will pass. These helpers centralise that seed so
# every matrix test file (runtime / admin) and the dedicated boundary
# tests use an identical shape.
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
    user_id: str | None = None,
    role_name: str,
    expires_at: datetime | None = None,
    principal_type: str = "user",
    principal_id: str | None = None,
) -> None:
    """Bind ``principal_id`` to the builtin ``role_name`` in ``org_id``.

    Polymorphic on ``principal_type`` (PR-034 added ``service_account``).
    Defaults preserve the pre-PR-034 ``bind_rbac_role(sf, user_id=..., role_name=...)``
    shape — when ``principal_type="user"`` and ``principal_id`` is
    omitted, the supplied ``user_id`` is used as the principal id. Pass
    ``principal_type="service_account"`` (and a ``principal_id``) to
    bind a ServiceAccount principal.

    Requires :func:`seed_rbac_builtin_roles` to have run first (the
    lookup is by ``(name, is_system)``).
    """
    from sqlalchemy import select

    from deerflow.persistence.iam.model import RoleBindingRow, RoleRow

    if principal_id is None:
        if user_id is None:
            raise ValueError("bind_rbac_role requires either principal_id or user_id")
        principal_id = user_id

    async with sf() as session:
        role = (await session.execute(select(RoleRow).where(RoleRow.name == role_name, RoleRow.is_system.is_(True)))).scalar_one()
        binding = RoleBindingRow(
            id=uuid4().hex,
            org_id=org_id,
            principal_type=principal_type,
            principal_id=principal_id,
            role_id=role.id,
            expires_at=expires_at,
        )
        session.add(binding)
        await session.commit()


async def seed_rbac_service_account(
    sf,
    *,
    org_id: str = RBAC_DEFAULT_ORG_ID,
    name: str = "sa-test",
    status: str = "active",
    owner_user_id: str | None = None,
    purpose: str | None = None,
    system: str | None = None,
    environment: str | None = None,
    expires_at: datetime | None = None,
):
    """Insert one ``ServiceAccountRow`` (PR-034). Returns the row.

    Idempotent on ``(org_id, name)``: if a row with the same name exists
    in the Org, returns it as-is (does NOT mutate its status/fields).
    """
    from sqlalchemy import select

    import deerflow.persistence.models  # noqa: F401 — register ORM
    from deerflow.persistence.iam.model import ServiceAccountRow

    async with sf() as session:
        existing = (
            await session.execute(
                select(ServiceAccountRow).where(
                    ServiceAccountRow.org_id == org_id,
                    ServiceAccountRow.name == name,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = ServiceAccountRow(
            id=uuid4().hex,
            org_id=org_id,
            name=name,
            status=status,
            owner_user_id=owner_user_id,
            purpose=purpose,
            system=system,
            environment=environment,
            expires_at=expires_at,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def bootstrap_rbac_service_account(
    sf,
    *,
    org_id: str = RBAC_DEFAULT_ORG_ID,
    name: str = "sa-test",
    role_name: str,
    status: str = "active",
):
    """One-shot IAM seed for a ServiceAccount principal (PR-034).

    Sister of :func:`bootstrap_rbac` for service principals. Seeds the
    Org + builtin roles + the ServiceAccount row + its role binding.
    Returns the :class:`ServiceAccountRow` (the caller needs its ``id``
    to construct a ``PrincipalRef(type="service_account", id=sa.id)``).
    """
    await seed_rbac_org(sf, org_id=org_id)
    await seed_rbac_builtin_roles(sf)
    sa = await seed_rbac_service_account(sf, org_id=org_id, name=name, status=status)
    await bind_rbac_role(
        sf,
        org_id=org_id,
        role_name=role_name,
        principal_type="service_account",
        principal_id=sa.id,
    )
    return sa


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
