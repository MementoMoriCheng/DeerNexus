"""Gateway tenant resolution adapter — single-Org bootstrap (PR-013).

Resolves a trusted :class:`TenantContext` after authentication and binds it so
downstream code operates inside a verified tenant scope (runtime-contracts.md
§5.2, api-boundaries §6.1 ``authenticate → resolve org membership → bind
TenantContext``).

Today this is the **single-Org bootstrap** mode (ADR-0001 §6): during
migration there is exactly one Organization, so every authenticated principal
resolves to the configured bootstrap org. This is deliberately temporary and
Feature-gated to single-Org; real Membership / OIDC-group resolution and the
second Org come later (PR-025, ADR-0003 §10).

Trust invariants enforced here:

* the resolver runs **after** :class:`AuthMiddleware` has authenticated the
  principal and stamped ``request.state.user`` / ``auth_source``;
* a client-supplied ``org_id`` (request body or header) is **never** the
  trusted source of truth — the org comes from the resolver, not the client
  (ADR-0002 §2.1; TM-001);
* binding always pairs with reset in ``try/finally`` so the contextvar is
  restored on both normal and exceptional exits;
* if no authenticated principal is present on a non-public path, the adapter
  fails closed (503) rather than silently proceeding without a tenant.

The middleware is registered after :class:`AuthMiddleware` so it sees the
authenticated user; it must run before route handlers so the tenant scope is
available to authorize / load / execute.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.gateway.config import get_gateway_config
from app.gateway.internal_auth import get_trusted_internal_owner_user_id
from deerflow.contracts import (
    AuthMethod,
    ContractError,
    ErrorCode,
    PrincipalRef,
    TenantContext,
    bind_tenant_context,
    reset_tenant_context,
)
from deerflow.tenancy import current_multi_org_phase

logger = logging.getLogger(__name__)

# Auth-source (internal gateway constant) → contract AuthMethod. The contract
# AuthMethod closed set has no "auth_disabled" value; local dev auth-disabled
# requests are treated as trusted-internal for the audit surface.
AUTH_SOURCE_SESSION = "session"
AUTH_SOURCE_INTERNAL = "internal"
AUTH_SOURCE_AUTH_DISABLED = "auth_disabled"

_BOOTSTRAP_AUTH_METHOD_MAP: dict[str, AuthMethod] = {
    AUTH_SOURCE_SESSION: "session",
    AUTH_SOURCE_INTERNAL: "internal",
    AUTH_SOURCE_AUTH_DISABLED: "internal",
}

# Paths that never require a tenant context (mirror AuthMiddleware public set).
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
)
_PUBLIC_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/auth/login/local",
        "/api/v1/auth/register",
        "/api/v1/auth/logout",
        "/api/v1/auth/setup-status",
        "/api/v1/auth/initialize",
    }
)


def _is_public(path: str) -> bool:
    stripped = path.rstrip("/")
    if stripped in _PUBLIC_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES)


def _resolve_request_id(request: Request) -> str:
    """Return a per-request correlation id, honouring an inbound ``X-Request-Id``."""
    inbound = request.headers.get("X-Request-Id")
    if inbound and inbound.strip():
        return inbound.strip()
    return uuid.uuid4().hex


def resolve_principal(user: object, request: Request) -> PrincipalRef:
    """Map an authenticated principal to a contract :class:`PrincipalRef`.

    For trusted internal calls carrying ``X-DeerFlow-Owner-User-Id`` the
    ``user_id`` is taken from that header (already trusted post-auth), naming
    the real owning user rather than the synthetic internal principal.
    """
    owner_user_id = get_trusted_internal_owner_user_id(request)
    user_id = owner_user_id if owner_user_id else str(getattr(user, "id"))
    return PrincipalRef(
        type="user",
        id=str(getattr(user, "id")),
        user_id=user_id,
        display_name=getattr(user, "email", None),
    )


async def resolve_tenant_context(
    user: object,
    auth_source: str,
    request_id: str,
    request: Request,
) -> TenantContext:
    """Resolve the trusted TenantContext for this request.

    Two resolution modes, gated on ``tenancy.multi_org.phase`` (read via
    :func:`deerflow.tenancy.current_multi_org_phase`):

    * ``disabled`` (default) — **single-Org bootstrap**: org id is the
      configured ``default_org_id``, never read from the request body. This is
      today's behaviour — zero DB cost, fully reversible. ``workspace_id`` is
      unset (single org, no workspace selection yet).
    * ``validation`` / ``active`` — **Membership-based**: the org id comes from
      the authenticated principal's active ``OrgMembership`` (queried via
      :func:`deerflow.tenancy.get_active_membership`), never from the request
      body and never synthesized (TEN-008). Single-membership-strict: zero
      active memberships or more than one both fail closed (raised → 503 by
      the middleware).

    The org id is **always** resolver-determined — a client-supplied org_id
    (request body or header) is never the trusted source of truth
    (ADR-0002 §2.1; TM-001).
    """
    config = get_gateway_config()
    auth_method = _BOOTSTRAP_AUTH_METHOD_MAP.get(auth_source, "internal")
    principal = resolve_principal(user, request)

    phase = current_multi_org_phase()
    if phase == "disabled":
        # Fast single-Org path: no DB, no await cost. Identical to pre-PR-025C+
        # behaviour so rolling the flag back is a pure config change.
        return TenantContext(
            org_id=config.default_org_id,
            principal=principal,
            auth_method=auth_method,
            request_id=request_id,
            issued_at=datetime.now(UTC),
        )

    # validation / active: resolve org from the principal's membership.
    # Deferred imports keep the disabled fast path free of persistence import
    # cost and mirror routers/auth.py's request-time import pattern.
    from deerflow.persistence.engine import get_session_factory
    from deerflow.tenancy import MultiMembershipError, get_active_membership

    sf = get_session_factory()
    if sf is None:
        # backend=memory (dev) has no ORM engine / membership data; multi-org
        # phases require persistence, so fail closed rather than fabricate.
        raise RuntimeError(f"tenancy.multi_org.phase={phase!r} requires persistence but no session factory is available (backend=memory does not support multi-org).")

    try:
        membership = await get_active_membership(sf, user_id=principal.user_id)
    except MultiMembershipError:
        raise  # surfaced as fail-closed 503 by the middleware wrapper
    if membership is None:
        raise RuntimeError(f"no active OrgMembership for principal user_id={principal.user_id!r} in phase={phase!r}; cannot bind a tenant context (TEN-008: never synthesize a default org).")

    return TenantContext(
        org_id=membership.org_id,
        principal=principal,
        auth_method=auth_method,
        request_id=request_id,
        issued_at=datetime.now(UTC),
    )


def resolve_channel_tenant_context(owner_user_id: str, request_id: str) -> TenantContext:
    """Resolve the trusted TenantContext for a channel dispatch (PR-014C).

    This is the Request-less counterpart of :func:`resolve_tenant_context`,
    serving the channel dispatch path (``ChannelManager._handle_message``)
    which has no HTTP request: it drives runs via HTTP loopback using the
    internal token + ``X-Deer-Flow-Owner-User-Id`` (see
    ``manager.py::_owner_headers``).

    * ``org_id`` comes from the configured bootstrap org — never synthesized
      from the message body (ADR-0002 §2.1; TM-001), mirroring the HTTP path;
    * ``principal`` is built from the trusted connection ``owner_user_id``
      (already resolved from the connection repo inside the manager); there is
      no user object, so ``id`` and ``user_id`` both name the owner;
    * ``auth_method`` is fixed to ``"internal"`` because channel-triggered
      runs always re-enter via the internal token (``_BOOTSTRAP_AUTH_METHOD_MAP``
      maps ``internal``→``internal`` the same way on the receiving side).
    """
    config = get_gateway_config()
    principal = PrincipalRef(
        type="user",
        id=owner_user_id,
        user_id=owner_user_id,
        display_name=None,
    )
    return TenantContext(
        org_id=config.default_org_id,
        principal=principal,
        auth_method="internal",
        request_id=request_id,
        issued_at=datetime.now(UTC),
    )


@contextmanager
def channel_tenant_scope(owner_user_id: str | None, request_id: str) -> Iterator[None]:
    """Bind a TenantContext for the duration of a channel dispatch (PR-014C).

    Complements (does not replace) the HTTP-loopback binding: the channel
    dispatch task itself becomes a tenant-scoped, auditable entry point per
    runtime-contracts.md §5.2 rule 3 (explicit, not implicit inheritance).

    When ``owner_user_id`` is ``None`` this is a no-op (mirrors
    ``manager.py::_owner_headers`` returning ``None``); the downstream HTTP
    loopback is the fail-closed gate for owner-less dispatches, so this scope
    binds only when there is a trusted owner and never synthesizes a default
    Org (§5.2 rule 6).

    Callers do not need to reset manually — the contextmanager restores the
    contextvar on both normal and exceptional exits (§5.2 rule 2).
    """
    if owner_user_id is None:
        yield
        return
    tenant = resolve_channel_tenant_context(owner_user_id, request_id)
    token = bind_tenant_context(tenant)
    try:
        yield
    finally:
        reset_tenant_context(token)


class TenantResolutionMiddleware(BaseHTTPMiddleware):
    """Resolve and bind the TenantContext after authentication.

    Registered after :class:`AuthMiddleware`. On non-public paths it reads the
    authenticated ``request.state.user`` / ``auth_source``, resolves the tenant
    and binds it via :func:`bind_tenant_context`, resetting in ``finally``. A
    missing authenticated principal fails closed (503) rather than proceeding
    without a tenant scope.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if _is_public(request.url.path):
            return await call_next(request)

        request_id = _resolve_request_id(request)
        request.state.request_id = request_id

        user = getattr(request.state, "user", None)
        auth_source = getattr(request.state, "auth_source", AUTH_SOURCE_SESSION)
        if user is None:
            # AuthMiddleware should have rejected this already; fail closed if
            # the stack is misconfigured rather than bind a tenant-less scope.
            err = ContractError.from_code(
                ErrorCode.AUTHENTICATION_INVALID,
                request_id=request_id,
                message="tenant resolver saw no authenticated principal on a non-public path",
            )
            return JSONResponse(status_code=503, content={"detail": err.model_dump()})

        try:
            tenant = await resolve_tenant_context(user, auth_source, request_id, request)
        except Exception as exc:  # noqa: BLE001 — surface any resolver failure as fail-closed
            err = ContractError.from_code(
                ErrorCode.AUTHENTICATION_INVALID,
                request_id=request_id,
                message=f"tenant resolution failed: {exc}",
            )
            return JSONResponse(status_code=503, content={"detail": err.model_dump()})

        logger.info(
            "tenant resolved",
            extra={
                "request_id": tenant.request_id,
                "org_id": tenant.org_id,
                "principal_type": tenant.principal.type,
                "principal_id": tenant.principal.id,
                "auth_method": tenant.auth_method,
            },
        )

        token = bind_tenant_context(tenant)
        try:
            return await call_next(request)
        finally:
            reset_tenant_context(token)
