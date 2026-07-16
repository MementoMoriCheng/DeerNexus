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
from collections.abc import Callable
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


def resolve_tenant_context(
    user: object,
    auth_source: str,
    request_id: str,
    request: Request,
) -> TenantContext:
    """Resolve the trusted TenantContext for this request (single-Org bootstrap).

    The org id is the configured bootstrap org — never read from the request
    body. ``workspace_id`` is unset (bootstrap has a single org, no workspace
    selection yet).
    """
    config = get_gateway_config()
    auth_method = _BOOTSTRAP_AUTH_METHOD_MAP.get(auth_source, "internal")
    return TenantContext(
        org_id=config.default_org_id,
        principal=resolve_principal(user, request),
        auth_method=auth_method,
        request_id=request_id,
        issued_at=datetime.now(UTC),
    )


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
            tenant = resolve_tenant_context(user, auth_source, request_id, request)
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
