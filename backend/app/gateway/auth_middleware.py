"""Global authentication middleware — fail-closed safety net.

Rejects unauthenticated requests to non-public paths with 401. When a
request passes the cookie check, resolves the JWT payload to a real
``User`` object and stamps it into both ``request.state.user`` and the
``deerflow.runtime.user_context`` contextvar so that repository-layer
owner filtering works automatically via the sentinel pattern.

Four credential sources are supported (tried in this order):

1. **API Key** (PR-035) — ``X-Api-Key: <plaintext>`` or
   ``Authorization: Bearer <plaintext>`` (plaintext starts with
   ``dk_live_``). Looks up by ``key_prefix``
   via the ``uq_api_keys_key_prefix`` unique index, constant-time
   verifies the HMAC hash, and rejects expired / revoked keys with 401.
   On success stamps a SA-backed stub user + ``AUTH_SOURCE_API_KEY`` +
   four ``request.state.api_key_*`` fields the tenant resolver reads.
2. **OIDC Bearer JWT** — ``Authorization: Bearer <JWT>`` where the token
   is a dot-separated JWT (not a ``dk_live_`` API key). Verifies the
   RS256 signature via the IdP JWKS endpoint, resolves the ``sub`` to a
   local user via ``external_identities``, and runs the group-mapping
   engine. Only active when ``production.oidc`` is configured; otherwise
   a non-API-key bearer token falls through to the session/internal paths.
3. **Internal token** — ``X-DeerFlow-Internal-Token`` (IM channel worker).
4. **Session cookie** — ``access_token`` JWT. Strict validation rejects
   junk/expired/stale tokens with 401.

Fine-grained permission checks live in the ``@require_rbac`` decorator
(``app.gateway.rbac``), which reads the bound ``TenantContext`` and
calls ``AuthorizeService.authorize()`` (PR-031). Authentication itself
is fully handled here.
"""

import asyncio
import logging
from collections.abc import Callable

from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.gateway.auth.api_key import verify_api_key
from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse
from app.gateway.auth.oidc import OidcTokenError
from app.gateway.auth_disabled import (
    AUTH_SOURCE_API_KEY,
    AUTH_SOURCE_AUTH_DISABLED,
    AUTH_SOURCE_INTERNAL,
    AUTH_SOURCE_OIDC,
    AUTH_SOURCE_SESSION,
    get_auth_disabled_user,
    is_auth_disabled,
)
from app.gateway.internal_auth import INTERNAL_AUTH_HEADER_NAME, get_internal_user, is_valid_internal_auth_token
from deerflow.contracts import ErrorCode
from deerflow.runtime.user_context import reset_current_user, set_current_user

logger = logging.getLogger(__name__)

# Paths that never require authentication.
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/health",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
)

# Exact auth paths that are public (login/register/status check).
# /api/v1/auth/me, /api/v1/auth/change-password etc. are NOT public.
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


def _strip_bearer(authorization_header: str | None) -> str:
    """Return the token from an ``Authorization: Bearer <token>`` header.

    Returns ``""`` if the header is missing or does not use the Bearer
    scheme. The token is returned as-is — format validation (API key
    vs JWT) is the caller's job. ADB: API-key plaintext always starts
    with ``dk_live_``; a JWT has dot separators. This helper does not
    distinguish so a future JWT-Bearer path can reuse it.
    """
    if not authorization_header:
        return ""
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token.strip()


def _unauthenticated_response() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "detail": AuthErrorResponse(
                code=AuthErrorCode.NOT_AUTHENTICATED,
                message="Authentication required",
            ).model_dump()
        },
    )


def _authentication_invalid_response() -> JSONResponse:
    """401 with the ADR §12 ``authentication_invalid`` code.

    Used for every API-key failure mode (missing key, unknown prefix,
    hash mismatch, expired, revoked, inconsistent state). The body is
    deliberately identical across failure modes so a caller cannot
    enumerate valid prefixes by comparing error messages.
    """
    return JSONResponse(
        status_code=401,
        content={
            "detail": AuthErrorResponse(
                code=AuthErrorCode.TOKEN_INVALID,
                message="Invalid API key",
            ).model_dump()
        },
    )


async def _resolve_api_key(request: Request) -> tuple[object, str] | None:
    """Try the API-key credential path. Returns ``(user_stub, auth_source)`` on success.

    Returns ``None`` if no API-key credential was presented (so the
    caller falls through to the session/internal paths). Raises
    ``_ApiKeyAuthError`` indirectly via the response returned from a
    failure — actually returns ``JSONResponse`` directly on any
    verification failure (the caller returns it immediately).

    Stamps on success (ADR §12 auth order: existence → hash → expiry →
    revocation → SA disabled):

    * ``request.state.user`` — a SimpleNamespace stand-in carrying
      ``id=sa.id`` so downstream ``getattr(user, "id")`` patterns work
      without a real ``User`` row.
    * ``request.state.auth_source = AUTH_SOURCE_API_KEY``
    * ``request.state.api_key_id``, ``request.state.api_key_org_id``,
      ``request.state.api_key_scopes``,
      ``request.state.service_account_id`` — read by
      :func:`app.gateway.tenant.resolve_tenant_context`.

    The plaintext key is NEVER logged here. The header value is
    discarded after ``verify_api_key`` returns.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace

    raw = request.headers.get("X-Api-Key") or _strip_bearer(request.headers.get("Authorization", ""))
    if not raw:
        return None

    # A bearer token that does NOT start with the API-key prefix is a
    # candidate OIDC JWT (dot-separated) — do not consume it here. When
    # OIDC is configured the dispatch() method routes it to the JWKS
    # path; when OIDC is not configured it falls through to the
    # session/internal paths (which ignore non-cookie bearer tokens).
    # The ``X-Api-Key`` header is always treated as an API key.
    if not request.headers.get("X-Api-Key") and not raw.startswith(_API_KEY_BEARER_PREFIX):
        return None

    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.iam import get_api_key_by_prefix, get_service_account, touch_api_key_last_used

    sf = get_session_factory()
    if sf is None:
        # No persistence wired (backend=memory dev) — API-key auth
        # requires the DB. Fail closed as auth_invalid; the session
        # cookie / auth-disabled paths may still admit the request.
        return _authentication_invalid_response()  # type: ignore[return-value]

    # Derive the DB key_prefix from the plaintext. ``generate_api_key``
    # builds the plaintext as ``dk_live_<random8>_<secret>``; the prefix
    # column stores the first 16 chars exactly.
    from app.gateway.auth.api_key import _DISPLAY_PREFIX, _RANDOM_PREFIX_LEN  # type: ignore[attr-defined]

    prefix_len = len(_DISPLAY_PREFIX) + _RANDOM_PREFIX_LEN
    if len(raw) < prefix_len:
        return _authentication_invalid_response()  # type: ignore[return-value]
    key_prefix = raw[:prefix_len]

    row = await get_api_key_by_prefix(sf, key_prefix=key_prefix)
    if row is None:
        return _authentication_invalid_response()  # type: ignore[return-value]

    # Constant-time hash compare. ADR §9.2 line 296.
    if not verify_api_key(raw, row.key_hash):
        return _authentication_invalid_response()  # type: ignore[return-value]

    # Expiry + revocation — both map to 401 authentication_invalid per
    # ADR §12 (revoked / expired Key → 401, NOT 403).
    #
    # SQLite strips tzinfo on round-trip, so the DB-returned timestamps
    # may be offset-naive. Coerce to UTC before comparing against
    # ``datetime.now(UTC)`` to avoid ``TypeError: can't compare
    # offset-naive and offset-aware datetimes``. Postgres preserves the
    # tzinfo and the coerce is a no-op there.
    now = datetime.now(UTC)
    expires_at = row.expires_at.astimezone(UTC) if row.expires_at.tzinfo is None else row.expires_at
    if expires_at <= now:
        return _authentication_invalid_response()  # type: ignore[return-value]
    revoked_at = row.revoked_at
    if revoked_at is not None:
        if revoked_at.tzinfo is None:
            revoked_at = revoked_at.astimezone(UTC)
        # ``revoked_at`` is monotonic; any non-None value means the key
        # is revoked, but compare to be defensive against a future where
        # revocation gets a "scheduled revoke" semantics (PR-035 Design A
        # — currently we only ever set revoked_at to "now").
        if revoked_at <= now:
            return _authentication_invalid_response()  # type: ignore[return-value]

    # ServiceAccount check — disabled → 403 principal_disabled.
    sa = await get_service_account(sf, service_account_id=row.service_account_id)
    if sa is None or sa.org_id != row.org_id:
        # Inconsistent state (Key references a missing / cross-Org SA).
        # Fail closed as auth_invalid rather than leak which side is wrong.
        return _authentication_invalid_response()  # type: ignore[return-value]
    if sa.status == "disabled":
        return JSONResponse(
            status_code=403,
            content={
                "detail": {
                    "code": ErrorCode.PRINCIPAL_DISABLED.value,
                    "message": "Principal is disabled",
                }
            },
        )

    # Success — stamp state for the tenant resolver + rbac decorator.
    request.state.user = SimpleNamespace(id=sa.id, email=None, system_role="user")
    request.state.auth_source = AUTH_SOURCE_API_KEY
    request.state.api_key_id = row.id  # type: ignore[attr-defined]
    request.state.api_key_org_id = row.org_id  # type: ignore[attr-defined]
    request.state.api_key_scopes = frozenset(row.scopes or [])  # type: ignore[attr-defined]
    request.state.service_account_id = sa.id  # type: ignore[attr-defined]

    # Fire-and-forget last_used_at touch (sampled — at most 1 write /
    # key / 60s). Observability column, not a correctness gate, so
    # swallow exceptions: a logging/DB hiccup must never fail an
    # otherwise-valid request.
    async def _touch() -> None:
        try:
            await touch_api_key_last_used(sf, api_key_id=row.id)
        except Exception:  # noqa: BLE001
            pass

    asyncio.create_task(_touch())

    return request.state.user, AUTH_SOURCE_API_KEY


#: API-key plaintext always starts with ``dk_live_``. A bearer token that
#: does NOT start with this prefix is a candidate JWT (dot-separated), and
#: when OIDC is configured it is verified via the JWKS path.
_API_KEY_BEARER_PREFIX = "dk_live_"


async def _resolve_oidc_bearer(request: Request, token: str) -> tuple[object, str] | JSONResponse | None:
    """Try the OIDC Bearer-JWT credential path.

    Returns ``None`` when OIDC is not configured (the token falls through
    to the session/internal paths), ``JSONResponse`` on a verification
    failure, or ``(user, AUTH_SOURCE_OIDC)`` on success.

    Only invoked for bearer tokens that are NOT API keys (i.e. do not
    start with ``dk_live_``). When ``production.oidc`` is ``None`` this
    function immediately returns ``None`` so the token is handled by the
    downstream session cookie logic (a JWT in the Authorization header
    is simply ignored when OIDC is not configured — the session path
    only reads the ``access_token`` cookie).
    """
    from deerflow.config.app_config import get_app_config

    oidc_config = getattr(get_app_config().production, "oidc", None)
    if oidc_config is None:
        # OIDC not configured — bearer token is not an API key and OIDC
        # is off; let the session/internal paths handle (or reject) it.
        return None

    from app.gateway.auth.oidc import verify_oidc_token

    claims = await asyncio.to_thread(verify_oidc_token, token, oidc_config)

    # Import here to avoid circular import at module level.
    from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse

    if isinstance(claims, OidcTokenError):
        # All OIDC failure modes map to the same token_invalid body
        # (anti-enumeration: a caller cannot distinguish "bad issuer"
        # from "bad signature" by comparing error messages).
        return JSONResponse(
            status_code=401,
            content={
                "detail": AuthErrorResponse(
                    code=AuthErrorCode.TOKEN_INVALID,
                    message="Invalid OIDC token",
                ).model_dump()
            },
        )

    # Resolve (issuer, sub) → local user via external_identities.
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.orgs.model import ExternalIdentityRow

    sf = get_session_factory()
    if sf is None:
        # No persistence wired (backend=memory dev) — OIDC requires the
        # DB for external-identity resolution. Fail closed.
        return _authentication_invalid_response()  # type: ignore[return-value]

    from sqlalchemy import select

    async with sf() as session:
        stmt = select(ExternalIdentityRow).where(
            ExternalIdentityRow.issuer == claims.iss,
            ExternalIdentityRow.subject == claims.sub,
        )
        ext_row = (await session.execute(stmt)).scalar_one_or_none()

    if ext_row is None:
        # The IdP authenticated a subject we have no binding for.
        # This is expected during onboarding (admin must pre-link the
        # external identity); fail closed rather than auto-provision.
        return JSONResponse(
            status_code=401,
            content={
                "detail": AuthErrorResponse(
                    code=AuthErrorCode.USER_NOT_FOUND,
                    message="OIDC subject not linked to a local user",
                ).model_dump()
            },
        )

    # Load the local user.
    from app.gateway.deps import get_local_provider

    provider = get_local_provider()
    user = await provider.get_user(ext_row.user_id)
    if user is None:
        return JSONResponse(
            status_code=401,
            content={
                "detail": AuthErrorResponse(
                    code=AuthErrorCode.USER_NOT_FOUND,
                    message="User not found",
                ).model_dump()
            },
        )

    # Run the additive group-mapping engine (idempotent — ensures
    # role_bindings match the IdP's latest group assertion).
    from deerflow.tenancy.oidc_group_mapping import apply_group_mapping

    try:
        await apply_group_mapping(
            sf,
            user_id=str(user.id),
            issuer=claims.iss,
            groups=claims.groups,
            subject=claims.sub,
            provider="oidc",
        )
    except Exception:  # noqa: BLE001 — group mapping failure must not block auth
        logger.warning("OIDC group mapping failed for user %s; auth proceeds", user.id, exc_info=True)

    from types import SimpleNamespace

    # Stamp a user stub compatible with the tenant resolver + RBAC
    # decorator patterns (same shape as the API-key stub user).
    request.state.user = SimpleNamespace(
        id=str(user.id),
        email=str(user.email) if user.email else None,
        system_role=user.system_role,
    )
    request.state.auth_source = AUTH_SOURCE_OIDC
    return request.state.user, AUTH_SOURCE_OIDC


class AuthMiddleware(BaseHTTPMiddleware):
    """Strict auth gate: reject requests without a valid session.

    Four credential paths tried in order (see module docstring). On
    success stamps ``request.state.user`` and the
    ``deerflow.runtime.user_context`` contextvar so repository-layer
    owner filters work downstream without every route needing its own
    authentication wrapper. Routes that need per-resource authorization
    additionally use ``@require_rbac(..., owner_check=True)``.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if _is_public(request.url.path):
            return await call_next(request)

        # API Key path (PR-035) — highest priority because it is the
        # external machine-identity entry point. Returns None when no
        # API-key credential was presented so the fall-through paths
        # run unchanged. Returns a JSONResponse directly on any
        # verification failure (401 / 403).
        api_key_result = await _resolve_api_key(request)
        if isinstance(api_key_result, JSONResponse):
            return api_key_result
        if api_key_result is not None:
            user, auth_source = api_key_result
            token = set_current_user(user)
            try:
                return await call_next(request)
            finally:
                reset_current_user(token)

        # OIDC Bearer-JWT path — tried after API key (which already
        # consumed ``dk_live_`` tokens) and before the internal/session
        # paths. Returns None when OIDC is not configured (the bearer
        # token is ignored by the session path, which only reads the
        # ``access_token`` cookie). Returns JSONResponse on verification
        # failure (401). On success stamps ``request.state`` + contextvar.
        bearer_token = _strip_bearer(request.headers.get("Authorization", ""))
        if bearer_token and not bearer_token.startswith(_API_KEY_BEARER_PREFIX):
            oidc_result = await _resolve_oidc_bearer(request, bearer_token)
            if isinstance(oidc_result, JSONResponse):
                return oidc_result
            if oidc_result is not None:
                user, auth_source = oidc_result
                token = set_current_user(user)
                try:
                    return await call_next(request)
                finally:
                    reset_current_user(token)

        internal_user = None
        if is_valid_internal_auth_token(request.headers.get(INTERNAL_AUTH_HEADER_NAME)):
            internal_user = get_internal_user()

        auth_source = AUTH_SOURCE_SESSION
        access_token = request.cookies.get("access_token")

        # Non-public path: require session cookie
        if internal_user is not None:
            user = internal_user
            auth_source = AUTH_SOURCE_INTERNAL
        elif access_token:
            # Strict JWT validation: reject junk/expired tokens with 401
            # right here instead of silently passing through. This closes
            # the "junk cookie bypass" gap (AUTH_TEST_PLAN test 7.5.8):
            # without this, non-isolation routes like /api/models would
            # accept any cookie-shaped string as authentication.
            #
            # We call the *strict* resolver so that fine-grained error
            # codes (token_expired, token_invalid, user_not_found, …)
            # propagate from AuthErrorCode, not get flattened into one
            # generic code. BaseHTTPMiddleware doesn't let HTTPException
            # bubble up, so we catch and render it as JSONResponse here.
            from app.gateway.deps import get_current_user_from_request

            try:
                user = await get_current_user_from_request(request)
            except HTTPException as exc:
                if not is_auth_disabled():
                    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
                user = get_auth_disabled_user()
                auth_source = AUTH_SOURCE_AUTH_DISABLED
        elif is_auth_disabled():
            user = get_auth_disabled_user()
            auth_source = AUTH_SOURCE_AUTH_DISABLED
        else:
            return _unauthenticated_response()

        # Stamp request.state.user (for the contextvar pattern).
        # TenantResolutionMiddleware runs next and binds the
        # TenantContext ContextVar that @require_rbac reads to call
        # AuthorizeService.authorize(). No legacy AuthContext is
        # stamped — fine-grained permission checks consult the
        # DB-backed Authorize Service, not an in-memory stub.
        request.state.user = user
        request.state.auth_source = auth_source
        token = set_current_user(user)
        try:
            return await call_next(request)
        finally:
            reset_current_user(token)
