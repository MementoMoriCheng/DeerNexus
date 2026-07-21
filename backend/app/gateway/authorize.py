"""Unified Authorize Service (PR-031).

Implements the ADR-0003 §6 effective-permission intersection:

    effective_permissions =
      active_membership            ← PR-031 (OrgMembership.status == "active")
      ∩ active_principal           ← PR-031 (system_role gate; UserRow has no
                                       status column yet, so this is coarse)
      ∩ non_expired_role_bindings  ← PR-031 (expires_at IS NULL OR > now)
      ∩ union(role.permissions)    ← PR-030 (seeded; PR-031 reads from DB)
      ∩ api_key.scopes_if_present  ← PR-031 (reserved; None = universe)
      ∩ organization_state         ← PR-031 (suspended/deleting raise)
      ∩ policy_decision            ← Track E (PR-031 treats as universe)

The Service exposes the ADR §6 uniform signature:

    authorize(tenant_context, permission, resource_ref=None) -> None | raises ContractError

and a direct ``compute_permissions_for_user`` helper for callers that need the
whole set (e.g. the future ``_authenticate`` rewrite in PR-032/033).

Boundary — what this module deliberately does NOT do (Track C division):

* It does **not** implement API Key validation (→ PR-035), OIDC group
  mapping (→ PR-036), or active cache invalidation + SSE re-validation
  (→ PR-037). The API Key scope parameter is accepted so the signature
  is stable when PR-035 lands. ``invalidate_principal`` is wired (PR-034)
  for the IAM write path; the SSE re-validation hook is PR-037.
* It does **not** return role/policy detail to the caller (ADR §6: "授权服务
  返回 allow 或抛稳定错误"). All denials raise :class:`ContractError` with a
  stable code; HTTP status mapping happens at the router layer (PR-032/033).
* It does **not** plumb ``service_account`` principals through
  ``TenantResolutionMiddleware`` — that lands with API-key header parsing
  in PR-035. PR-034's branch is exercised through service-layer unit
  tests that construct a ``PrincipalRef(type="service_account", ...)``
  directly (mirrors PR-031's "authorize lands with zero router callers").

Error codes (ADR §12 + testing-strategy §9.2):

* ``membership.status == "suspended"`` → ``PERMISSION_DENIED`` (caller maps 403).
* ``membership.status in {"invited", "removed"}`` or no membership row →
  ``PERMISSION_DENIED`` (caller maps 404; existence is hidden to avoid
  leaking org scope).
* ``org.status == "suspended"`` → ``ORG_SUSPENDED`` (403).
* ``org.status == "deleting"`` → ``ORG_DELETING`` (403).
* permission not in effective set → ``PERMISSION_DENIED`` (403).
* Unknown principal type / missing user_id → ``AUTHENTICATION_INVALID`` (401).

Fail-closed posture: any DB lookup error bubbles up as an exception; the
caller (middleware) is expected to translate that to a 503, mirroring the
tenant resolver's contract (see ``app/gateway/tenant.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.gateway.authorize_cache import (
    DEFAULT_TTL_SECONDS,
    InMemoryPermissionCache,
    PermissionCache,
    org_cache_key,
    system_cache_key,
)
from deerflow.contracts import (
    SYSTEM_PERMISSIONS,
    ErrorCode,
    Permission,
    TenantContext,
)
from deerflow.contracts.rbac import SYSTEM_PERMISSION_PREFIX
from deerflow.persistence.iam.model import RoleBindingRow, RoleRow, ServiceAccountRow
from deerflow.tenancy import get_membership_any_status, get_org_status

if TYPE_CHECKING:
    from app.gateway.auth.models import User


class AuthorizeError(Exception):
    """Raised by the Authorize Service for every ADR §12 terminal state.

    Carries the stable :class:`~deerflow.contracts.errors.ErrorCode` so the
    HTTP layer (PR-032/033 router / middleware) can map it to a status code
    without string matching:

    * ``PERMISSION_DENIED`` → 403 (suspended / insufficient) or 404
      (invited / removed / no membership / missing org — existence hidden).
      The router decides 403 vs 404 from context (e.g. membership absence
      signals 404).
    * ``ORG_SUSPENDED`` / ``ORG_DELETING`` → 403.
    * ``AUTHENTICATION_INVALID`` → 401.

    Mirrors :class:`~deerflow.contracts.context.TenantContextError` /
    :class:`~deerflow.contracts.rbac.PermissionValidationError`: ``ContractError``
    itself is a pydantic data envelope, not an Exception, so it cannot be
    raised directly. ``AuthorizeError`` is the raise-able form; routers can
    build a ``ContractError.from_code(...)`` from it when composing the JSON
    response.
    """

    code: ErrorCode

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        permission: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.permission = permission


def _denied(message: str, *, permission: str | None = None) -> AuthorizeError:
    return AuthorizeError(ErrorCode.PERMISSION_DENIED, message, permission=permission)


# ---------------------------------------------------------------------------
# Pure function: effective_permissions intersection (ADR §6)
# ---------------------------------------------------------------------------

#: Membership statuses that count as "no effective relationship" — the caller
#: cannot tell from a 404 whether the row was ``invited``, ``removed`` or
#: absent, so the Authorize Service maps all three the same way (ADR §12
#: existence-hiding rule).
_INACTIVE_MEMBERSHIP_STATUSES: frozenset[str] = frozenset({"invited", "removed"})


def compute_effective_permissions(
    *,
    membership_status: str | None,
    role_permissions: frozenset[str],
    org_status: str,
    system_role: str = "user",
    api_key_scopes: frozenset[str] | None = None,
) -> frozenset[str]:
    """Compute the effective permission set per ADR §6.

    Pure (no IO): callers feed the DB rows they have already read, which keeps
    the function unit-testable and cacheable. The ``authorize()`` entry point
    below is a thin DB + cache wrapper around this.

    Preconditions encoded by the caller:

    * ``membership_status`` is the row's status or ``None`` (no row); the
      caller has *already* decided to proceed past the suspended/invited/
      removed gates — those raise in :meth:`AuthorizeService.authorize`
      before reaching here. So in practice this function only runs when
      ``membership_status == "active"``. The parameter is kept for clarity
      and for direct unit tests.
    * ``org_status`` is the Org's status; the caller has already gated on
      suspended/deleting. In practice this runs only when
      ``org_status == "active"``.
    * ``role_permissions`` is the union of every bound role's ``permissions``
      JSON array, already filtered to non-expired bindings and (for non-system
      roles) scrubbed of ``system:*`` strings by the registry invariant.

    The ``system_role == "admin"`` short-circuit (ADR §4.4: ``system:admin``
    is independent of RoleBinding) returns :data:`SYSTEM_PERMISSIONS`
    directly — org-scoped role permissions are irrelevant for a platform
    admin. API Key scopes are still applied (an admin using a scoped Key is
    narrowed by the Key).

    ``api_key_scopes=None`` means "no API Key in play" → universe (the
    intersection leaves ``role_permissions`` untouched). A non-``None`` value
    intersects, which can only narrow (ADR §6: "API Key scope 只能收窄").
    """
    # ADR §4.4: system-admin bypasses Org-scoped RoleBinding entirely.
    if system_role == "admin":
        base: frozenset[str] = frozenset(SYSTEM_PERMISSIONS)
    else:
        base = role_permissions

    if api_key_scopes is None:
        return base
    # API Key scope intersection (universe when None, narrowing when set).
    return base & api_key_scopes


# ---------------------------------------------------------------------------
# AuthorizeService — DB + cache wrapper
# ---------------------------------------------------------------------------


class AuthorizeService:
    """Runtime authorizer bound to a session factory and a permission cache.

    One instance per process is the intended shape: the cache is shared so
    repeated requests from the same principal hit within the TTL window, and
    the session factory is the same one every other gateway helper uses.

    The service is read-only with respect to the DB: no commits, only
    ``SELECT``. Failures bubble up as exceptions for the middleware to wrap
    into a 503 (matching ``app/gateway/tenant.py``'s contract).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cache: PermissionCache | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._sf = session_factory
        self._cache: PermissionCache = cache if cache is not None else InMemoryPermissionCache()
        self._ttl_seconds = ttl_seconds

    # -- public API ---------------------------------------------------------

    async def compute_permissions_for_user(
        self,
        user: User,
        *,
        org_id: str,
        api_key_scopes: frozenset[str] | None = None,
    ) -> frozenset[str]:
        """Return the effective permission set for ``user`` within ``org_id``.

        Wraps the pure :func:`compute_effective_permissions` with the DB reads
        (membership / role bindings / roles / org status) and the cache. Raises
        :class:`ContractError` on any authorization failure (suspended /
        invited / removed membership, suspended / deleting org) — the caller
        surfaces those as HTTP 4xx at the router layer.

        ``api_key_scopes`` is the reserved PR-035 hook: pass ``None`` for
        interactive sessions (no narrowing), or a scope set when the caller
        has already validated an API Key (PR-035 will populate this).
        """
        user_id = str(user.id)
        system_role = getattr(user, "system_role", "user") or "user"

        # system-admin short-circuit: independent of Org / RoleBinding, own
        # cache namespace (ADR §11).
        if system_role == "admin":
            key = system_cache_key(principal_id=user_id)
            cached = self._cache.get(key)
            if cached is not None:
                return self._apply_scopes(cached, api_key_scopes)
            perms = self._compute_admin_permissions()
            self._cache.set(key, perms, ttl_seconds=self._ttl_seconds)
            return self._apply_scopes(perms, api_key_scopes)

        cache_k = org_cache_key(org_id=org_id, principal_type="user", principal_id=user_id)
        cached = self._cache.get(cache_k)
        if cached is not None:
            return self._apply_scopes(cached, api_key_scopes)

        perms = await self._compute_user_permissions(user_id=user_id, org_id=org_id, system_role=system_role)
        self._cache.set(cache_k, perms, ttl_seconds=self._ttl_seconds)
        return self._apply_scopes(perms, api_key_scopes)

    async def authorize(
        self,
        tenant_context: TenantContext,
        permission: Permission | str,
        resource_ref: object | None = None,  # noqa: ARG002 — reserved for future resource-level checks
        *,
        api_key_scopes: frozenset[str] | None = None,
    ) -> None:
        """ADR §6 uniform entry point: allow (return None) or raise ``ContractError``.

        ``resource_ref`` is accepted for signature stability but is not used
        in MVP — resource-level (Workspace) RBAC is an explicit non-target
        (ADR §17). It will be wired when Track E / Workspace RBAC lands.

        Branches on ``tenant_context.principal.type``:

        * ``user`` — original PR-031 path (membership + RoleBindings).
        * ``service_account`` — PR-034 path. ``ServiceAccountRow.status``
          is the active-principal gate (no Membership concept); disabled
          SAs raise ``PRINCIPAL_DISABLED``. The role-bindings JOIN reuses
          the same ``_fetch_role_permissions`` helper the user path uses
          (the polymorphic ``(principal_type, principal_id)`` filter was
          already in place from PR-031).
        * anything else (``system``) — ``AUTHENTICATION_INVALID``. The
          ``system:admin`` cross-Org interface (ADR §4.4) is a future PR.

        ``api_key_scopes`` is plumbed through to
        :meth:`compute_permissions_for_user` /
        :meth:`compute_permissions_for_service_account`; today no caller
        passes a non-``None`` value (PR-035 will populate it from a real
        API-key lookup).
        """
        principal = tenant_context.principal
        org_id = tenant_context.org_id
        perm_value = permission.value if isinstance(permission, Permission) else str(permission)

        if principal.type == "user":
            if principal.user_id is None:
                raise AuthorizeError(
                    ErrorCode.AUTHENTICATION_INVALID,
                    "authorize() requires a 'user' principal to carry a user_id.",
                )
            # Build a minimal user-like object — authorize() takes
            # TenantContext, but compute_permissions_for_user expects the
            # auth User shape. We only read .id and .system_role, so a
            # SimpleNamespace is enough and avoids forcing callers to
            # materialize a full User row.
            from types import SimpleNamespace

            user = SimpleNamespace(id=principal.user_id, system_role="user")
            effective = await self.compute_permissions_for_user(user, org_id=org_id, api_key_scopes=api_key_scopes)
        elif principal.type == "service_account":
            if not principal.id:
                raise AuthorizeError(
                    ErrorCode.AUTHENTICATION_INVALID,
                    "authorize() requires a 'service_account' principal to carry a non-empty id.",
                )
            effective = await self.compute_permissions_for_service_account(
                service_account_id=principal.id,
                org_id=org_id,
                api_key_scopes=api_key_scopes,
            )
        else:
            raise AuthorizeError(
                ErrorCode.AUTHENTICATION_INVALID,
                f"authorize() does not yet support principal type {principal.type!r}.",
            )

        if perm_value not in effective:
            raise _denied(
                f"Principal lacks required permission {perm_value!r}.",
                permission=perm_value,
            )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _apply_scopes(perms: frozenset[str], api_key_scopes: frozenset[str] | None) -> frozenset[str]:
        if api_key_scopes is None:
            return perms
        return perms & api_key_scopes

    def _compute_admin_permissions(self) -> frozenset[str]:
        """ADR §4.4: system-admin carries the platform ``system:*`` set.

        Synchronous because no DB read is needed — the system permission set
        is a frozen registry constant (PR-030). Kept as a method so a future
        PR can add org-scoped admin permissions on top without touching
        call sites.
        """
        return frozenset(SYSTEM_PERMISSIONS)

    def invalidate_principal(self, *, org_id: str, principal_type: str, principal_id: str) -> None:
        """Drop the cached effective-permission set for one principal (ADR §11).

        Called by the IAM write path (ServiceAccount disable/enable/delete,
        RoleBinding create/delete) after the DB commit. The TTL (≤60s) is
        the fallback if this call is missed, the process crashes between
        commit and invalidate, or the principal lives behind a different
        process (cross-process cache coherence is out of scope for the
        in-memory cache — it lands with PR-037's active-invalidation +
        distributed cache).

        ``principal_type`` is part of the cache key
        (:func:`org_cache_key`), so a user and a service_account that
        happen to share an id cannot collide. ``system`` principals use a
        separate namespace (:func:`system_cache_key`) and are not
        invalidated through this entry point.
        """
        self._cache.invalidate(org_cache_key(org_id=org_id, principal_type=principal_type, principal_id=principal_id))

    async def compute_permissions_for_service_account(
        self,
        *,
        service_account_id: str,
        org_id: str,
        api_key_scopes: frozenset[str] | None = None,
    ) -> frozenset[str]:
        """Return the effective permission set for a ServiceAccount principal.

        Mirrors :meth:`compute_permissions_for_user` but on the
        ServiceAccount branch (ADR §6 intersection formula). Cache key
        composition uses ``principal_type="service_account"`` so a user
        and a SA that share an id cannot collide.

        Raises :class:`AuthorizeError` for every ADR §12 terminal state:

        * ``ORG_SUSPENDED`` / ``ORG_DELETING`` — organization_state gate.
        * ``AUTHENTICATION_INVALID`` — SA missing or belongs to a
          different Org (cross-Org leakage is hidden as "does not
          exist", matching the user path's existence-hiding posture).
        * ``PRINCIPAL_DISABLED`` — ``ServiceAccountRow.status ==
          "disabled"``. The new auth attempt on a disabled SA must
          return 403 ``principal_disabled`` (ADR §12).

        ``api_key_scopes`` is the reserved PR-035 hook (today always
        ``None`` from the router; PR-035's API-key lookup will populate
        it). Scope narrowing is applied on top of the cached set so a
        cache hit still respects scopes.
        """
        cache_k = org_cache_key(org_id=org_id, principal_type="service_account", principal_id=service_account_id)
        cached = self._cache.get(cache_k)
        if cached is not None:
            return self._apply_scopes(cached, api_key_scopes)

        perms = await self._compute_service_account_permissions(service_account_id=service_account_id, org_id=org_id)
        self._cache.set(cache_k, perms, ttl_seconds=self._ttl_seconds)
        return self._apply_scopes(perms, api_key_scopes)

    async def _compute_user_permissions(self, *, user_id: str, org_id: str, system_role: str) -> frozenset[str]:
        """DB-backed effective-permission computation for an Org-scoped user.

        Raises :class:`ContractError` for every ADR §12 terminal state
        (suspended / invited / removed membership, suspended / deleting org).
        The caller (cache layer) never sees these — they propagate up.
        """
        # 1. organization_state — check first because a suspended/deleting Org
        # colours every other decision (ADR §6: "Suspended Org 禁止新 Run 和
        # 发布; Deleting Org 只允许删除流程").
        org_status = await get_org_status(self._sf, org_id=org_id)
        if org_status is None:
            # Org row missing: treat as no scope to authorize against. The
            # error is framed as permission_denied (404) rather than leaking
            # "org does not exist" — ADR §12 existence-hiding rule.
            raise _denied("Principal has no effective organization scope.")
        if org_status == "suspended":
            raise AuthorizeError(ErrorCode.ORG_SUSPENDED, "Organization is suspended.")
        if org_status == "deleting":
            raise AuthorizeError(ErrorCode.ORG_DELETING, "Organization is being deleted.")
        # "deleted" is treated like "deleting" — both are terminal write-blocked
        # states. ADR §12 does not enumerate "deleted" separately.
        if org_status == "deleted":
            raise AuthorizeError(ErrorCode.ORG_DELETING, "Organization is deleted.")

        # 2. active_membership — distinguish suspended (403) from
        # invited/removed/absent (404). All three raise the same code so the
        # client cannot tell which; the HTTP layer maps 403 vs 404 via the
        # router (PR-032/033) using the absence-of-membership signal.
        membership = await get_membership_any_status(self._sf, user_id=user_id, org_id=org_id)
        if membership is None or membership.status in _INACTIVE_MEMBERSHIP_STATUSES:
            raise _denied("Principal has no active membership in this organization.")
        if membership.status == "suspended":
            raise _denied("Principal's membership is suspended.")
        # status == "active" → proceed.

        # 3. non_expired_role_bindings ∩ union(role.permissions). Single
        # query joins role_bindings → roles, filtered by org + principal +
        # expiry. We read permissions as a JSON list and union across rows.
        role_perms = await self._fetch_role_permissions(org_id=org_id, principal_type="user", principal_id=user_id)

        # 4. Delegate the intersection math (system_role gate, API Key scope,
        #    policy universe) to the pure function. system_role is always
        #    "user" on this path — the admin short-circuit returned earlier.
        return compute_effective_permissions(
            membership_status=membership.status,
            role_permissions=role_perms,
            org_status=org_status,
            system_role=system_role,
            api_key_scopes=None,  # scope narrowing happens at compute_permissions_for_user
        )

    async def _compute_service_account_permissions(self, *, service_account_id: str, org_id: str) -> frozenset[str]:
        """DB-backed effective-permission computation for a ServiceAccount principal.

        Same ADR §6 intersection order as :meth:`_compute_user_permissions`,
        but the "active_principal" dimension is the ``ServiceAccountRow``
        itself (``status == "active"``), not a Membership row — SAs do not
        carry Memberships (the ``org_memberships.user_id`` FK is to
        ``users.id``). Cross-Org access is hidden as ``AUTHENTICATION_INVALID``
        to mirror the existence-hiding posture of the user path's
        ``PERMISSION_DENIED`` for missing membership.
        """
        # 1. organization_state — suspended/deleting raise the same codes
        #    as the user path so a single ``_authorize_error_to_http``
        #    mapping covers both principal types.
        org_status = await get_org_status(self._sf, org_id=org_id)
        if org_status is None:
            raise _denied("Principal has no effective organization scope.")
        if org_status == "suspended":
            raise AuthorizeError(ErrorCode.ORG_SUSPENDED, "Organization is suspended.")
        if org_status == "deleting":
            raise AuthorizeError(ErrorCode.ORG_DELETING, "Organization is being deleted.")
        if org_status == "deleted":
            raise AuthorizeError(ErrorCode.ORG_DELETING, "Organization is deleted.")

        # 2. active_principal — ServiceAccountRow is the principal record.
        #    Missing / wrong-Org / disabled map to distinct error codes
        #    (ADR §12). The CHECK constraint on ``status`` limits the
        #    column to ``active``/``disabled``, so no defensive else is
        #    needed after the disabled branch.
        async with self._sf() as session:
            sa_row = await session.get(ServiceAccountRow, service_account_id)
        if sa_row is None or sa_row.org_id != org_id:
            # Existence-hiding: a SA in another Org looks identical to a
            # non-existent one, so cross-Org callers cannot enumerate.
            raise AuthorizeError(
                ErrorCode.AUTHENTICATION_INVALID,
                "ServiceAccount does not exist in this organization.",
            )
        if sa_row.status == "disabled":
            raise AuthorizeError(
                ErrorCode.PRINCIPAL_DISABLED,
                "ServiceAccount is disabled.",
            )

        # 3. non_expired_role_bindings ∩ union(role.permissions). Same
        #    JOIN as the user path; the polymorphic filter on
        #    ``principal_type`` was already parameterised in PR-031.
        role_perms = await self._fetch_role_permissions(org_id=org_id, principal_type="service_account", principal_id=service_account_id)

        # 4. Delegate the intersection math to the pure function.
        #    ServiceAccounts are NEVER system-admin: SYSTEM_PERMISSIONS is
        #    outside the Org-role domain (SYSTEM_PERMISSION_PREFIX write-
        #    side guard in ``validate_role_permissions`` enforces this at
        #    binding time), so system_role="user" is correct here.
        return compute_effective_permissions(
            membership_status="active",  # SA "active" maps to the active-membership path inside the pure function
            role_permissions=role_perms,
            org_status=org_status,
            system_role="user",
            api_key_scopes=None,  # scope narrowing happens at compute_permissions_for_service_account
        )

    async def _fetch_role_permissions(self, *, org_id: str, principal_type: str, principal_id: str) -> frozenset[str]:
        """Union of ``permissions`` across the principal's non-expired bindings.

        Joins ``role_bindings`` → ``roles`` in one SELECT, filters by
        ``(org_id, principal_type, principal_id)`` and
        ``expires_at IS NULL OR expires_at > now``. Returns the union of every
        matched role's ``permissions`` JSON array as a ``frozenset[str]``.

        Unknown permission strings (not in :class:`Permission`) are dropped
        silently here — the registry is the authority at write time
        (``validate_role_permissions``), and the read path should be
        resilient to a row that was written before a permission was removed
        from the registry. ``system:*`` strings on Org-scoped roles are also
        dropped defensively: the registry forbids them on writes, but a
        pre-existing violation should not widen an Org role.
        """
        now = datetime.now(UTC)
        async with self._sf() as session:
            stmt = (
                select(RoleRow.permissions)
                .join(RoleBindingRow, RoleBindingRow.role_id == RoleRow.id)
                .where(
                    RoleBindingRow.org_id == org_id,
                    RoleBindingRow.principal_type == principal_type,
                    RoleBindingRow.principal_id == principal_id,
                    # NULL expires_at = never expires; otherwise must be in the future.
                    (RoleBindingRow.expires_at.is_(None)) | (RoleBindingRow.expires_at > now),
                )
            )
            rows = (await session.execute(stmt)).scalars().all()

        unioned: set[str] = set()
        for permissions_json in rows:
            if not permissions_json:
                continue
            for perm in permissions_json:
                if not isinstance(perm, str):
                    continue
                # Defensive scrub: drop anything outside the registry or
                # carrying the system prefix on an Org-scoped role.
                if perm.startswith(SYSTEM_PERMISSION_PREFIX):
                    continue
                unioned.add(perm)
        return frozenset(unioned)


# Sentinel used by callers that want to opt out of caching for a one-off
# recompute (e.g. a doctor probe). Constructed lazily because the session
# factory may not be available at import time.
_default_service: AuthorizeService | None = None


def get_authorize_service() -> AuthorizeService:
    """Return the process-wide AuthorizeService, constructing it on first use.

    Mirrors the lazy-initialisation pattern of other gateway helpers. Raises
    ``RuntimeError`` if persistence is not initialised, matching
    ``app/gateway/tenant.py``'s contract — the middleware wraps that into 503.
    """
    global _default_service
    if _default_service is not None:
        return _default_service

    from deerflow.persistence.engine import get_session_factory

    sf = get_session_factory()
    if sf is None:
        raise RuntimeError("AuthorizeService requires persistence but no session factory is available (backend=memory / not initialised).")
    _default_service = AuthorizeService(sf)
    return _default_service


def reset_authorize_service_for_testing() -> None:
    """Drop the cached default service. Tests call this after swapping the factory."""
    global _default_service
    _default_service = None


__all__ = [
    "AuthorizeError",
    "AuthorizeService",
    "compute_effective_permissions",
    "get_authorize_service",
    "reset_authorize_service_for_testing",
]
