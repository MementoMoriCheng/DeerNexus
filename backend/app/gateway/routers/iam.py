"""IAM ServiceAccount API (PR-034) + API Key endpoints (PR-035) + OIDC group mapping (PR-036) + OrgMembership lifecycle (PR-037).

Twenty endpoints mounted at ``/api/v1/iam``:

ServiceAccount lifecycle (PR-034):

* ``GET    /service-accounts``                       — list (Org-scoped)
* ``POST   /service-accounts``                       — create
* ``GET    /service-accounts/{sa_id}``               — get
* ``PATCH  /service-accounts/{sa_id}``               — update traceability fields
* ``POST   /service-accounts/{sa_id}:disable``       — lifecycle: active → disabled
* ``POST   /service-accounts/{sa_id}:enable``        — lifecycle: disabled → active
* ``DELETE /service-accounts/{sa_id}``               — lifecycle: → deleted (hard)
* ``GET    /service-accounts/{sa_id}/role-bindings`` — list bindings
* ``POST   /service-accounts/{sa_id}/role-bindings`` — bind a role
* ``DELETE /service-accounts/{sa_id}/role-bindings/{binding_id}`` — unbind

API Key lifecycle (PR-035):

* ``POST   /service-accounts/{sa_id}/api-keys``          — mint (returns plaintext ONCE)
* ``GET    /service-accounts/{sa_id}/api-keys``          — list (no plaintext, no hash)
* ``DELETE /service-accounts/{sa_id}/api-keys/{key_id}`` — revoke (idempotent)

OIDC group mapping (PR-036, ADR-0003 §10):

* ``GET    /oidc-group-mappings``                 — list allowlist (Org-scoped)
* ``POST   /oidc-group-mappings``                 — create allowlist entry
* ``PATCH  /oidc-group-mappings/{id}``            — update (group_claim/value/role/mode/description)
* ``DELETE /oidc-group-mappings/{id}``            — remove entry (idempotent)
* ``POST   /oidc-group-mappings:preview``         — dry-run preview against the caller

OrgMembership lifecycle (PR-037, ADR-0003 §7 + §11):

* ``POST   /org-memberships/{user_id}:suspend``   — lifecycle: active → suspended (revocation)
* ``POST   /org-memberships/{user_id}:activate``   — lifecycle: suspended → active

Gating (ADR §4): all reads use ``Permission.ADMIN_IAM_READ``, all writes
use ``Permission.ADMIN_IAM_MANAGE``. Both are carried only by
``org:admin`` (PR-030 registry pin); developer / viewer receive 403 via
``@require_rbac`` + ``AuthorizeService.authorize()``.

Lifecycle state machine (ADR §9.1):

    active ↔ disabled
    active | disabled → deleted

Deletion is hard (no tombstone) and runs in a single transaction with
role-binding cleanup and api-key CASCADE (ADR §12 "ServiceAccount 删除
必须与全部 Key 撤销在同一受控事务完成").

API Key rules (ADR §9.2):

* Plaintext is returned exactly once on mint; the DB stores only
  ``key_hash`` (HMAC-SHA256(pepper, plaintext)). The read path never
  surfaces plaintext or hash.
* ``scopes`` is required, non-empty, and must be a subset of the
  registry's ``Permission`` values (no ``system:*``). Scope narrowing
  is applied per-request at the ``authorize()`` boundary, NOT cached
  per-key, so Key create/revoke do not invalidate the SA's cache.
* Rotation = create new + revoke old within 24h (ADR §9.2 ≤24h overlap)
  — there is no dedicated ``:rotate`` endpoint; the audit log shows a
  ``api_key_created`` + ``api_key_revoked`` pair.

Audit: every mutation enqueues an AuditEvent into ``audit_outbox`` in the
SAME transaction as the business write (ADR §7.1, PR-042) — the outbox row
and the IAM row commit atomically, so a failed enqueue rolls back the write
(no "business success without an audit row"). Actions are normalized to the
``<domain>.<resource>.<verb>`` registry (ADR §4) via :func:`build_audit_event`
+ :func:`enqueue_audit_outbox_in_session`. Cache invalidation runs AFTER the
commit via :meth:`AuthorizeService.invalidate_principal` (ADR §11).

Cross-Org isolation (ADR §8 "列表与查询强制 Org 过滤"): every endpoint
takes the caller's ``org_id`` from the bound ``TenantContext`` and
returns 404 (not 403) for a SA that exists in another Org —
existence-hiding, matching the posture established in PR-031/032/033.

What this router deliberately does NOT do (PR boundary):

* It does not emit Class B runtime-security events (login deny / policy
  deny / sandbox violation) — those are ADR §7.2, PR-044.
* It does not implement rate limiting (ADR §9.3) — platform limiter PR.
* It does not emit a dedicated ``api_key_rotated`` event — rotation is
  the composition of ``api_key_created`` + ``api_key_revoked`` (≤24h).

OIDC group-mapping rules (ADR-0003 §10):

* The set of mapping rows IS the allowlist — an unmatched
  ``(issuer, group)`` is never mapped (§10 rule 1).
* ``additive`` is the MVP default; ``authoritative`` is stored but the
  mapping service refuses to enact it (§10 "authoritative 模式需单独
  启用") — the column exists so a future mode can switch on without a
  schema change.
* A target role carrying any ``system:*`` permission is rejected at
  create/update (§10 rule 3) — the router looks up the role and
  validates via ``validate_role_permissions``.
* The ``:preview`` dry-run always runs against the CALLER (never an
  arbitrary target user) to avoid a "dry-run as reconnaissance" abuse
  vector.
* Last-admin protection (ADR §7) is a service-layer primitive
  (``assert_not_last_admin``); additive mapping never removes a binding,
  so the protection is exercised by the future removal path, not here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app.gateway.auth.api_key import generate_api_key
from app.gateway.authorize import get_authorize_service
from app.gateway.rbac import require_rbac
from deerflow.contracts import Permission, get_tenant_context
from deerflow.contracts.iam import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyResponse,
    OidcGroupMappingCreateRequest,
    OidcGroupMappingResponse,
    OidcGroupMappingUpdateRequest,
    OidcMappingPreviewRequest,
    OidcMappingPreviewResponse,
    OrgMembershipResponse,
    ServiceAccountCreateRequest,
    ServiceAccountResponse,
    ServiceAccountRoleBindingRequest,
    ServiceAccountRoleBindingResponse,
    ServiceAccountUpdateRequest,
)
from deerflow.contracts.identity import PrincipalRef
from deerflow.contracts.policy import ResourceRef
from deerflow.contracts.rbac import SYSTEM_PERMISSION_PREFIX, validate_role_permissions
from deerflow.persistence.audit import enqueue_audit_outbox_in_session
from deerflow.persistence.iam import (
    MEMBERSHIP_ACTIVE,
    MEMBERSHIP_SUSPENDED,
    SERVICE_ACCOUNT_ACTIVE,
    SERVICE_ACCOUNT_DISABLED,
    create_api_key,
    create_oidc_group_mapping,
    create_role_binding,
    create_service_account,
    delete_oidc_group_mapping,
    delete_role_binding,
    delete_service_account,
    get_api_key,
    get_membership,
    get_oidc_group_mapping,
    get_service_account,
    list_api_keys,
    list_oidc_group_mappings,
    list_role_bindings,
    list_service_accounts,
    revoke_api_key,
    set_membership_status,
    set_service_account_status,
    update_oidc_group_mapping,
    update_service_account,
)
from deerflow.tenancy.audit_events import build_audit_event
from deerflow.tenancy.oidc_group_mapping import (
    LastAdminError,
    apply_group_mapping,
    assert_not_last_admin,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/iam", tags=["iam"])


def _require_org_id(request: Request) -> str:
    """Resolve the caller's active ``org_id`` from the bound TenantContext.

    Mirrors ``admin._require_org_id``'s posture: IAM is per-Org and an
    anonymous / no-tenant request has no business here, so we fail
    closed with 400 instead of fabricating a default Org. The
    ``@require_rbac`` decorator has already enforced authentication +
    ``admin:iam:read``/``manage`` before this runs.
    """
    ctx = get_tenant_context()
    if ctx is None or not ctx.org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="IAM API requires an active tenant context; none is bound.",
        )
    return ctx.org_id


def _actor_id(request: Request) -> str | None:
    """Return the calling user's id for audit attribution, or ``None``.

    The ``@require_rbac`` decorator guarantees ``request.state.user`` is
    set by the time the handler runs; the ``None`` branch is defensive
    for the unit-test direct-call path.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        return None
    return str(user.id)


def _audit_actor(request: Request) -> PrincipalRef:
    """Build the audit ``PrincipalRef`` for the authenticated caller.

    The ``@require_rbac`` decorator has already authenticated the caller, so
    ``request.state.user`` carries a real user id in production. The ``None``
    branch (no bound user) is the defensive test / direct-call path: it
    attributes the event to the ``system`` principal so the audit record is
    never missing an actor. ``user_id`` is only set for genuine ``user``
    principals (PrincipalRef validator enforces this).
    """
    user_id = _actor_id(request)
    if user_id is not None:
        return PrincipalRef(type="user", id=user_id, user_id=user_id)
    return PrincipalRef(type="system", id="system")


def _audit_resource(*, type_: str, id_: str | None, org_id: str) -> ResourceRef:
    """Build a tenant-scoped ``ResourceRef`` for an audit event (ADR §3)."""
    return ResourceRef(type=type_, id=id_, org_id=org_id)


async def _emit_class_a_audit(
    session,
    *,
    action: str,
    org_id: str,
    actor: PrincipalRef,
    resource: ResourceRef,
    payload: dict | None = None,
) -> None:
    """Same-transaction Class A audit enqueue (ADR §7.1).

    Builds the ``AuditEvent`` and adds a ``pending`` outbox row to ``session``
    WITHOUT committing — the caller's ``session.commit()`` lands both the
    business write and this audit row atomically (or rolls back both on
    failure). The class-A guarantee "no business success without an audit
    row" is structural: a failed enqueue raises inside the shared
    transaction and aborts it.
    """
    event = build_audit_event(
        action,
        org_id=org_id,
        actor=actor,
        outcome="success",
        resource=resource,
        payload=payload or {},
    )
    await enqueue_audit_outbox_in_session(session, event)


def _to_response(row) -> ServiceAccountResponse:
    return ServiceAccountResponse.model_validate(row)


def _to_binding_response(row) -> ServiceAccountRoleBindingResponse:
    return ServiceAccountRoleBindingResponse.model_validate(row)


# ---------------------------------------------------------------------------
# ServiceAccount lifecycle
# ---------------------------------------------------------------------------


@router.get("/service-accounts", response_model=list[ServiceAccountResponse])
@require_rbac(Permission.ADMIN_IAM_READ)
async def list_org_service_accounts(request: Request) -> list[ServiceAccountResponse]:
    """List all ServiceAccounts in the caller's Org (ADR §8 Org filter)."""
    org_id = _require_org_id(request)
    rows = await list_service_accounts(_sf(request), org_id=org_id)
    return [_to_response(r) for r in rows]


@router.post("/service-accounts", response_model=ServiceAccountResponse, status_code=status.HTTP_201_CREATED)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def create_org_service_account(
    request: Request,
    body: ServiceAccountCreateRequest,
) -> ServiceAccountResponse:
    """Create a new ServiceAccount in the caller's Org.

    Initial ``status`` is always ``active``; use the ``:disable`` endpoint
    to transition. ``409 Conflict`` if ``(org_id, name)`` already exists.
    """
    org_id = _require_org_id(request)
    actor = _actor_id(request)
    sf = _sf(request)
    # Class A same-transaction write (ADR §7.1): business insert + audit
    # outbox enqueue commit atomically. An IntegrityError (name collision)
    # raises before the outbox row exists, so a rejected write produces no
    # audit event.
    async with sf() as session:
        try:
            row = await create_service_account(
                sf,
                org_id=org_id,
                name=body.name,
                description=body.description,
                owner_user_id=body.owner_user_id,
                purpose=body.purpose,
                system=body.system,
                environment=body.environment,
                expires_at=body.expires_at,
                created_by=actor,
                session=session,
            )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"ServiceAccount named {body.name!r} already exists in this organization.",
            ) from exc
        await _emit_class_a_audit(
            session,
            action="iam.service_account.created",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="service_account", id_=row.id, org_id=org_id),
            payload={"sa_id": row.id, "name": row.name, "owner_user_id": row.owner_user_id},
        )
        await session.commit()
    return _to_response(row)


@router.get("/service-accounts/{sa_id}", response_model=ServiceAccountResponse)
@require_rbac(Permission.ADMIN_IAM_READ)
async def get_org_service_account(request: Request, sa_id: str) -> ServiceAccountResponse:
    """Get one ServiceAccount. Cross-Org → 404 (existence-hiding)."""
    org_id = _require_org_id(request)
    row = await get_service_account(_sf(request), service_account_id=sa_id)
    if row is None or row.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    return _to_response(row)


@router.patch("/service-accounts/{sa_id}", response_model=ServiceAccountResponse)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def update_org_service_account(
    request: Request,
    sa_id: str,
    body: ServiceAccountUpdateRequest,
) -> ServiceAccountResponse:
    """Update traceability fields. ``status`` is NOT patchable here."""
    org_id = _require_org_id(request)
    sf = _sf(request)
    fields = body.model_dump(exclude_unset=True)
    # Class A same-transaction write (ADR §7.1).
    async with sf() as session:
        try:
            row = await update_service_account(sf, service_account_id=sa_id, session=session, **fields)
        except ValueError as exc:
            # Either an unknown field (defensive — pydantic extra=forbid catches
            # this earlier) or the row is missing. Treat both as 404 for
            # existence-hiding.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.") from exc
        if row.org_id != org_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
        await _emit_class_a_audit(
            session,
            action="iam.service_account.updated",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="service_account", id_=row.id, org_id=org_id),
            payload={"sa_id": row.id, "fields": sorted(fields)},
        )
        await session.commit()
    return _to_response(row)


@router.post("/service-accounts/{sa_id}:disable", response_model=ServiceAccountResponse)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def disable_org_service_account(request: Request, sa_id: str) -> ServiceAccountResponse:
    """Lifecycle: active → disabled. New auth on a disabled SA → 403 (ADR §12)."""
    return await _transition_status(request, sa_id, SERVICE_ACCOUNT_DISABLED)


@router.post("/service-accounts/{sa_id}:enable", response_model=ServiceAccountResponse)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def enable_org_service_account(request: Request, sa_id: str) -> ServiceAccountResponse:
    """Lifecycle: disabled → active."""
    return await _transition_status(request, sa_id, SERVICE_ACCOUNT_ACTIVE)


async def _transition_status(request: Request, sa_id: str, target: str) -> ServiceAccountResponse:
    org_id = _require_org_id(request)
    sf = _sf(request)
    action_verb = "disabled" if target == SERVICE_ACCOUNT_DISABLED else "activated"
    # Class A same-transaction write (ADR §7.1).
    async with sf() as session:
        try:
            row = await set_service_account_status(sf, service_account_id=sa_id, status=target, session=session)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.") from exc
        if row.org_id != org_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
        await _emit_class_a_audit(
            session,
            action=f"iam.service_account.{action_verb}",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="service_account", id_=row.id, org_id=org_id),
            payload={"sa_id": row.id, "status": target},
        )
        await session.commit()
    # The SA's own cache entry may be live; drop it so the next auth
    # attempt picks up the new status (ADR §11 SLO ≤60s, active
    # invalidation is the preferred path). Post-commit: a rolled-back
    # transition (outbox write failure) never reaches here.
    get_authorize_service().invalidate_principal(org_id=org_id, principal_type="service_account", principal_id=sa_id)
    return _to_response(row)


@router.delete("/service-accounts/{sa_id}", status_code=status.HTTP_204_NO_CONTENT)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def delete_org_service_account(request: Request, sa_id: str) -> None:
    """Lifecycle: → deleted. Hard-delete + same-transaction binding/key cleanup.

    ADR §12: SA deletion MUST land in the same transaction as full Key
    revocation. The repository helper does both inside one ``AsyncSession``;
    api_keys CASCADE via FK, role_bindings are DELETEd explicitly
    (polymorphic, no FK).
    """
    org_id = _require_org_id(request)
    sf = _sf(request)
    existing = await get_service_account(sf, service_account_id=sa_id)
    if existing is None or existing.org_id != org_id:
        # Existence-hiding: identical 404 for "missing" and "wrong Org".
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    # Class A same-transaction write (ADR §7.1): the delete returns the
    # pre-delete row so the audit payload carries ``id`` / ``name``, then
    # the outbox enqueue + the DELETE commit atomically. A failed enqueue
    # rolls back the delete too (no half-deleted SA).
    async with sf() as session:
        pre_delete = await delete_service_account(sf, service_account_id=sa_id, session=session)
        if pre_delete is None:
            # Re-entrant delete after a concurrent deletion: nothing to audit.
            await session.commit()
            return
        await _emit_class_a_audit(
            session,
            action="iam.service_account.deleted",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="service_account", id_=pre_delete.id, org_id=org_id),
            payload={"sa_id": pre_delete.id, "name": pre_delete.name},
        )
        await session.commit()
    get_authorize_service().invalidate_principal(org_id=org_id, principal_type="service_account", principal_id=sa_id)


# ---------------------------------------------------------------------------
# Role bindings
# ---------------------------------------------------------------------------


@router.get(
    "/service-accounts/{sa_id}/role-bindings",
    response_model=list[ServiceAccountRoleBindingResponse],
)
@require_rbac(Permission.ADMIN_IAM_READ)
async def list_sa_role_bindings(request: Request, sa_id: str) -> list[ServiceAccountRoleBindingResponse]:
    """List role bindings for a SA (Org-scoped)."""
    org_id = _require_org_id(request)
    sa = await get_service_account(_sf(request), service_account_id=sa_id)
    if sa is None or sa.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    rows = await list_role_bindings(_sf(request), org_id=org_id, principal_type="service_account", principal_id=sa_id)
    return [_to_binding_response(r) for r in rows]


@router.post(
    "/service-accounts/{sa_id}/role-bindings",
    response_model=ServiceAccountRoleBindingResponse,
    status_code=status.HTTP_201_CREATED,
)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def create_sa_role_binding(
    request: Request,
    sa_id: str,
    body: ServiceAccountRoleBindingRequest,
) -> ServiceAccountRoleBindingResponse:
    """Bind a role to a SA. ``409 Conflict`` if the binding already exists."""
    org_id = _require_org_id(request)
    actor = _actor_id(request)
    sf = _sf(request)
    sa = await get_service_account(sf, service_account_id=sa_id)
    if sa is None or sa.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    # Class A same-transaction write (ADR §7.1).
    async with sf() as session:
        try:
            row = await create_role_binding(
                sf,
                org_id=org_id,
                principal_type="service_account",
                principal_id=sa_id,
                role_id=body.role_id,
                created_by=actor,
                expires_at=body.expires_at,
                session=session,
            )
        except IntegrityError as exc:
            # Either the (org, principal, role) tuple already exists, or
            # role_id does not point at a real roles row (FK violation).
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Role binding already exists or role_id is invalid.",
            ) from exc
        await _emit_class_a_audit(
            session,
            action="iam.role_binding.created",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="role_binding", id_=row.id, org_id=org_id),
            payload={"sa_id": sa_id, "binding_id": row.id, "role_id": body.role_id},
        )
        await session.commit()
    get_authorize_service().invalidate_principal(org_id=org_id, principal_type="service_account", principal_id=sa_id)
    return _to_binding_response(row)


@router.delete(
    "/service-accounts/{sa_id}/role-bindings/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def delete_sa_role_binding(request: Request, sa_id: str, binding_id: str) -> None:
    """Remove a role binding. Idempotent: 204 even if the binding is gone."""
    org_id = _require_org_id(request)
    sf = _sf(request)
    sa = await get_service_account(sf, service_account_id=sa_id)
    if sa is None or sa.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    # Class A same-transaction write (ADR §7.1): the delete returns the
    # pre-delete row (carrying role_id) for the audit payload, then the
    # outbox enqueue + DELETE commit atomically.
    async with sf() as session:
        pre_delete = await delete_role_binding(sf, binding_id=binding_id, org_id=org_id, session=session)
        if pre_delete is not None:
            await _emit_class_a_audit(
                session,
                action="iam.role_binding.deleted",
                org_id=org_id,
                actor=_audit_actor(request),
                resource=_audit_resource(type_="role_binding", id_=binding_id, org_id=org_id),
                payload={"sa_id": sa_id, "binding_id": binding_id, "role_id": pre_delete.role_id},
            )
        await session.commit()
    get_authorize_service().invalidate_principal(org_id=org_id, principal_type="service_account", principal_id=sa_id)


# ---------------------------------------------------------------------------
# API Key lifecycle (PR-035)
# ---------------------------------------------------------------------------
#
# ADR §9.2 governs the Key rules. The plaintext is returned EXACTLY ONCE
# from the mint endpoint and never persisted; the DB stores only
# ``key_hash = HMAC-SHA256(pepper, plaintext)``. ``scopes`` is required
# and validated against the Permission registry (no ``system:*``). The
# cache is NOT invalidated on Key create/revoke because scope narrowing
# happens AFTER the cache boundary in
# :meth:`AuthorizeService.compute_permissions_for_service_account` —
# the cached value is the SA's full pre-scope set, unaffected by any
# Key mutation. The revoke path still calls ``invalidate_principal``
# defensively (matches ADR §11 line "API Key ... 变更主动失效"
# letter-for-letter and future-proofs against a refactor).


def _validate_scopes(scopes: list[str]) -> None:
    """Reject empty / unknown / system-prefixed scope strings (ADR §9.2)."""
    if not scopes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="API Key scopes must be non-empty (ADR §9.2).",
        )
    for scope in scopes:
        if not isinstance(scope, str) or not scope:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"API Key scope entry {scope!r} is invalid.",
            )
        if scope.startswith(SYSTEM_PERMISSION_PREFIX):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"API Key scope {scope!r} carries the system: prefix; system permissions cannot be granted to a ServiceAccount.",
            )
    try:
        validate_role_permissions(scopes, is_system=False)
    except Exception as exc:  # noqa: BLE001 — PermissionValidationError carries a stable code
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"API Key scope validation failed: {exc}",
        ) from exc


def _to_api_key_response(row) -> ApiKeyResponse:
    return ApiKeyResponse.model_validate(row)


@router.post(
    "/service-accounts/{sa_id}/api-keys",
    response_model=ApiKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def mint_sa_api_key(
    request: Request,
    sa_id: str,
    body: ApiKeyCreateRequest,
) -> ApiKeyCreateResponse:
    """Mint a new API Key. The plaintext is returned EXACTLY ONCE.

    ADR §9.2: ``scopes`` must be non-empty + subset of the SA's
    effective permissions. The plaintext is generated server-side,
    returned in this response, and never persisted — only its HMAC
    lands in ``api_keys.key_hash``. ``409 Conflict`` on a ``key_prefix``
    collision (retried once internally; surfacing 409 means a genuine
    random collision or a misbehaving RNG).
    """
    org_id = _require_org_id(request)
    sf = _sf(request)
    sa = await get_service_account(sf, service_account_id=sa_id)
    if sa is None or sa.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    _validate_scopes(body.scopes)

    # Mint + insert. Retry once on a prefix collision (2^48 space,
    # collision probability is negligible; the retry keeps the user-facing
    # 409 for a *repeated* collision which would indicate a real bug). The
    # prefix-collision retry runs in a throwaway session (rolled back) so the
    # successful insert + the Class A audit enqueue commit atomically in the
    # final session (ADR §7.1).
    plaintext, key_prefix, key_hash = generate_api_key()

    async def _attempt_insert(session):
        return await create_api_key(
            sf,
            org_id=org_id,
            service_account_id=sa_id,
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=body.scopes,
            expires_at=body.expires_at,
            session=session,
        )

    row = None
    async with sf() as session:
        try:
            row = await _attempt_insert(session)
        except IntegrityError:
            await session.rollback()
            plaintext, key_prefix, key_hash = generate_api_key()
            try:
                row = await _attempt_insert(session)
            except IntegrityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="API Key prefix collision after retry; please retry the request.",
                ) from exc
        # Class A same-transaction audit enqueue (ADR §7.1). The payload MUST
        # NOT contain the plaintext or the hash (ADR §9.2 line 302).
        await _emit_class_a_audit(
            session,
            action="iam.api_key.created",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="api_key", id_=row.id, org_id=org_id),
            payload={
                "key_id": row.id,
                "key_prefix": row.key_prefix,
                "sa_id": sa_id,
                "scopes": list(body.scopes),
                "expires_at": body.expires_at.isoformat(),
            },
        )
        await session.commit()

    return ApiKeyCreateResponse(
        id=row.id,
        org_id=row.org_id,
        service_account_id=row.service_account_id,
        key_prefix=row.key_prefix,
        scopes=list(row.scopes),
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        plaintext_key=plaintext,
    )


@router.get(
    "/service-accounts/{sa_id}/api-keys",
    response_model=list[ApiKeyResponse],
)
@require_rbac(Permission.ADMIN_IAM_READ)
async def list_sa_api_keys(request: Request, sa_id: str) -> list[ApiKeyResponse]:
    """List API Keys for a SA. NEVER returns plaintext or hash."""
    org_id = _require_org_id(request)
    sa = await get_service_account(_sf(request), service_account_id=sa_id)
    if sa is None or sa.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    rows = await list_api_keys(_sf(request), org_id=org_id, service_account_id=sa_id)
    return [_to_api_key_response(r) for r in rows]


@router.delete(
    "/service-accounts/{sa_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def revoke_sa_api_key(request: Request, sa_id: str, key_id: str) -> None:
    """Revoke an API Key. Idempotent: 204 even if the key is already revoked.

    Sets ``revoked_at = now`` (the row is retained for audit). ADR §9.2
    line 299 forbids un-revoking; the absence of an un-revoke endpoint
    enforces this structurally.

    Cross-Org: a key under a SA in another Org looks identical to a
    missing key (404), matching the existence-hiding posture.
    """
    org_id = _require_org_id(request)
    sf = _sf(request)
    sa = await get_service_account(sf, service_account_id=sa_id)
    if sa is None or sa.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    # Confirm the key belongs to this SA in this Org before revoking —
    # otherwise a caller could revoke a foreign-Org key by guessing the id.
    key = await get_api_key(sf, api_key_id=key_id)
    if key is None or key.org_id != org_id or key.service_account_id != sa_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API Key not found.")
    # Class A same-transaction write (ADR §7.1): revoke + audit enqueue commit
    # atomically. A revoke is idempotent (revoked_at is monotonic); the audit
    # event is emitted on every call so the audit trail records each revoke
    # attempt even if the row was already revoked.
    async with sf() as session:
        await revoke_api_key(sf, api_key_id=key_id, org_id=org_id, session=session)
        await _emit_class_a_audit(
            session,
            action="iam.api_key.revoked",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="api_key", id_=key_id, org_id=org_id),
            payload={"key_id": key_id, "key_prefix": key.key_prefix, "sa_id": sa_id},
        )
        await session.commit()
    # Defensive invalidation (no-op per cache-walk-through, but matches
    # ADR §11 "API Key ... 变更主动失效" wording letter-for-letter).
    get_authorize_service().invalidate_principal(org_id=org_id, principal_type="service_account", principal_id=sa_id)


# ---------------------------------------------------------------------------
# OIDC group mapping (PR-036) — ADR-0003 §10
# ---------------------------------------------------------------------------
#
# The mapping rows ARE the allowlist (§10 rule 1). CRUD is Org-scoped on
# ``target_org_id``; the engine (``apply_group_mapping``) is the apply
# path invoked by the real OIDC login (a future PR) and by the
# ``:preview`` dry-run below. Rule 3 (no system permissions) is enforced
# at create/update by looking up the target role.


async def _validate_mapping_target_role(request: Request, role_id: str) -> None:
    """ADR §10 rule 3: reject a target role carrying any ``system:*`` permission.

    Also rejects an unknown role_id (the mapping engine's own defence-in-
    depth read would skip it, but failing at config-write time gives a
    clearer 400 than a silent skip at apply time). Looks up the role via
    a one-shot session because ``get_service_account``-style helpers do
    not exist for roles (roles are read through the bootstrap / authorize
    paths today, not a dedicated repository reader).
    """
    from sqlalchemy import select

    from deerflow.persistence.iam.model import RoleRow

    sf = _sf(request)
    async with sf() as session:
        role = (await session.execute(select(RoleRow).where(RoleRow.id == role_id))).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"target_role_id {role_id!r} does not reference a known role.",
        )
    perms = role.permissions or []
    if any(isinstance(p, str) and p.startswith(SYSTEM_PERMISSION_PREFIX) for p in perms):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_role carries a system: permission; OIDC groups cannot map to system permissions (ADR §10 rule 3).",
        )
    # Belt-and-braces: validate the role's declared permission set is
    # well-formed (the registry is authoritative at write time; a mapping
    # must not reference a role whose perms have drifted into the unknown).
    try:
        validate_role_permissions(perms, is_system=bool(role.is_system))
    except Exception as exc:  # noqa: BLE001 — PermissionValidationError carries a stable code
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"target_role permission set is invalid: {exc}",
        ) from exc


def _validate_mapping_mode(mode: str) -> None:
    """Reject an unknown ``mode`` value (the CHECK constraint would also catch it)."""
    if mode not in ("additive", "authoritative"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"mode must be 'additive' or 'authoritative' (got {mode!r}).",
        )


def _to_mapping_response(row) -> OidcGroupMappingResponse:
    return OidcGroupMappingResponse.model_validate(row)


@router.get(
    "/oidc-group-mappings",
    response_model=list[OidcGroupMappingResponse],
)
@require_rbac(Permission.ADMIN_IAM_READ)
async def list_oidc_group_mappings_route(request: Request) -> list[OidcGroupMappingResponse]:
    """List the OIDC group-mapping allowlist for the caller's Org (ADR §8 Org filter)."""
    org_id = _require_org_id(request)
    rows = await list_oidc_group_mappings(_sf(request), org_id=org_id)
    return [_to_mapping_response(r) for r in rows]


@router.post(
    "/oidc-group-mappings",
    response_model=OidcGroupMappingResponse,
    status_code=status.HTTP_201_CREATED,
)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def create_oidc_group_mapping_route(
    request: Request,
    body: OidcGroupMappingCreateRequest,
) -> OidcGroupMappingResponse:
    """Create one allowlist entry (ADR §10 config model).

    Validates rule 3 (target role has no system perms) before persisting.
    ``409 Conflict`` if ``(issuer, group_value, target_org_id, target_role_id)``
    already exists (duplicate allowlist entry).
    """
    org_id = _require_org_id(request)
    actor = _actor_id(request)
    sf = _sf(request)
    if body.target_org_id != org_id:
        # An admin may only create mappings targeting their OWN org — a
        # cross-org target would let an admin in Org A inject bindings
        # into Org B via the mapping engine.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_org_id must match the caller's active org.",
        )
    _validate_mapping_mode(body.mode)
    await _validate_mapping_target_role(request, body.target_role_id)
    # Class A same-transaction write (ADR §7.1).
    async with sf() as session:
        try:
            row = await create_oidc_group_mapping(
                sf,
                issuer=body.issuer,
                group_claim=body.group_claim,
                group_value=body.group_value,
                target_org_id=body.target_org_id,
                target_role_id=body.target_role_id,
                mode=body.mode,
                description=body.description,
                created_by=actor,
                session=session,
            )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An OIDC group mapping for this (issuer, group, org, role) already exists.",
            ) from exc
        await _emit_class_a_audit(
            session,
            action="iam.oidc_group_mapping.created",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="oidc_group_mapping", id_=row.id, org_id=org_id),
            payload={
                "mapping_id": row.id,
                "issuer": row.issuer,
                "group_value": row.group_value,
                "target_role_id": row.target_role_id,
                "mode": row.mode,
            },
        )
        await session.commit()
    return _to_mapping_response(row)


@router.patch(
    "/oidc-group-mappings/{mapping_id}",
    response_model=OidcGroupMappingResponse,
)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def update_oidc_group_mapping_route(
    request: Request,
    mapping_id: str,
    body: OidcGroupMappingUpdateRequest,
) -> OidcGroupMappingResponse:
    """Update an allowlist entry. ``issuer`` / ``target_org_id`` are immutable.

    Re-validates rule 3 when ``target_role_id`` changes. Cross-Org → 404
    (existence-hiding). ``409`` if the update would collide with an
    existing ``(issuer, group_value, org, role)`` tuple.
    """
    org_id = _require_org_id(request)
    sf = _sf(request)
    fields = body.model_dump(exclude_unset=True)
    if "mode" in fields:
        _validate_mapping_mode(fields["mode"])
    if "target_role_id" in fields:
        await _validate_mapping_target_role(request, fields["target_role_id"])
    existing = await get_oidc_group_mapping(sf, mapping_id=mapping_id)
    if existing is None or existing.target_org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC group mapping not found.")
    # Class A same-transaction write (ADR §7.1).
    async with sf() as session:
        try:
            row = await update_oidc_group_mapping(sf, mapping_id=mapping_id, session=session, **fields)
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Update collides with an existing (issuer, group, org, role) mapping.",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC group mapping not found.") from exc
        await _emit_class_a_audit(
            session,
            action="iam.oidc_group_mapping.updated",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="oidc_group_mapping", id_=row.id, org_id=org_id),
            payload={"mapping_id": row.id, "fields": sorted(fields)},
        )
        await session.commit()
    return _to_mapping_response(row)


@router.delete(
    "/oidc-group-mappings/{mapping_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def delete_oidc_group_mapping_route(request: Request, mapping_id: str) -> None:
    """Remove one allowlist entry. Idempotent: 204 even if already gone.

    Deleting a mapping rule does NOT revoke any bindings it previously
    materialized (ADR §10 rule 6: additive mapping never removes; the
    ``created_by`` provenance on those bindings is retained). A future
    authoritative sweep would remove them explicitly.
    """
    org_id = _require_org_id(request)
    sf = _sf(request)
    existing = await get_oidc_group_mapping(sf, mapping_id=mapping_id)
    if existing is None or existing.target_org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC group mapping not found.")
    # Class A same-transaction write (ADR §7.1): delete returns the pre-delete
    # row so the audit payload carries the mapping identity; the DELETE +
    # outbox enqueue commit atomically.
    async with sf() as session:
        pre_delete = await delete_oidc_group_mapping(sf, mapping_id=mapping_id, org_id=org_id, session=session)
        if pre_delete is not None:
            await _emit_class_a_audit(
                session,
                action="iam.oidc_group_mapping.deleted",
                org_id=org_id,
                actor=_audit_actor(request),
                resource=_audit_resource(type_="oidc_group_mapping", id_=mapping_id, org_id=org_id),
                payload={
                    "mapping_id": pre_delete.id,
                    "issuer": pre_delete.issuer,
                    "group_value": pre_delete.group_value,
                    "target_role_id": pre_delete.target_role_id,
                },
            )
        await session.commit()


@router.post(
    "/oidc-group-mappings:preview",
    response_model=OidcMappingPreviewResponse,
)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def preview_oidc_group_mapping_route(
    request: Request,
    body: OidcMappingPreviewRequest,
) -> OidcMappingPreviewResponse:
    """Dry-run preview: what would the mapping engine apply for the caller?

    Runs ``apply_group_mapping(..., dry_run=True)`` against the CALLER's
    own ``user_id`` + active membership. Never writes. The preview is
    caller-scoped (no ``user_id`` in the request body) so an admin cannot
    use it as a reconnaissance tool against another user.
    """
    _require_org_id(request)
    actor = _actor_id(request)
    if actor is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Preview requires an authenticated caller; the dry-run runs against your own identity.",
        )
    result = await apply_group_mapping(
        _sf(request),
        user_id=actor,
        issuer=body.issuer,
        groups=body.groups,
        dry_run=True,
    )
    return OidcMappingPreviewResponse(
        user_id=result.user_id,
        issuer=result.issuer,
        dry_run=result.dry_run,
        planned=[{"group_value": o.group_value, "target_role_id": o.target_role_id, "target_org_id": o.target_org_id, "applied": o.applied, "reason": o.reason} for o in result.planned],
        applied=[{"group_value": o.group_value, "target_role_id": o.target_role_id, "target_org_id": o.target_org_id, "applied": o.applied, "reason": o.reason} for o in result.applied],
        skipped=[{"group_value": o.group_value, "target_role_id": o.target_role_id, "target_org_id": o.target_org_id, "applied": o.applied, "reason": o.reason} for o in result.skipped],
    )


# ---------------------------------------------------------------------------
# OrgMembership lifecycle (PR-037) — ADR-0003 §7 + §11
# ---------------------------------------------------------------------------
#
# suspend/activate are the revocation write path §11's SLO measures:
# commit the status change → invalidate the principal's authz cache →
# the next request (and any in-flight SSE re-validation) sees the denial
# within the ≤60s bound. Suspend of the sole ``org:admin`` is refused by
# ``assert_not_last_admin`` (ADR §7); activate is always permitted.


async def _org_admin_role_id(sf, *, org_id: str) -> str | None:
    """Return the id of the system-template ``org:admin`` role, or ``None``.

    Used by the last-admin guard before a suspend. Looked up by
    ``(name='org:admin', is_system=True)`` — the builtin system template
    seeded by ``ensure_builtin_roles`` (PR-030).
    """
    from sqlalchemy import select

    from deerflow.persistence.iam.model import RoleRow
    from deerflow.tenancy.bootstrap import SYSTEM_ADMIN_ROLE_NAME

    async with sf() as session:
        role = (await session.execute(select(RoleRow).where(RoleRow.name == SYSTEM_ADMIN_ROLE_NAME, RoleRow.is_system.is_(True)))).scalar_one_or_none()
    return role.id if role is not None else None


def _to_membership_response(row) -> OrgMembershipResponse:
    return OrgMembershipResponse.model_validate(row)


@router.post(
    "/org-memberships/{user_id}:suspend",
    response_model=OrgMembershipResponse,
)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def suspend_org_member(request: Request, user_id: str) -> OrgMembershipResponse:
    """Lifecycle: active → suspended. ADR §7 revocation.

    A suspended membership is the authorization revocation mechanism
    (ADR §11): the next ``authorize()`` after this commit denies. The
    router invalidates the principal's authz cache post-commit so the
    SLO holds immediately. Suspending the sole ``org:admin`` is refused
    (``assert_not_last_admin`` → 409) — emergency removal is the
    system-admin dedicated flow.
    """
    org_id = _require_org_id(request)
    sf = _sf(request)
    # Last-admin guard: refuse if this user is the sole active org:admin.
    admin_role_id = await _org_admin_role_id(sf=sf, org_id=org_id)
    if admin_role_id is not None:
        try:
            await assert_not_last_admin(sf=sf, org_id=org_id, role_id=admin_role_id, principal_id=user_id)
        except LastAdminError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
    # Class A same-transaction write (ADR §7.1).
    async with sf() as session:
        try:
            row = await set_membership_status(sf, org_id=org_id, user_id=user_id, status=MEMBERSHIP_SUSPENDED, session=session)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found.") from exc
        await _emit_class_a_audit(
            session,
            action="iam.membership.suspended",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="org_membership", id_=row.id, org_id=org_id),
            payload={"user_id": user_id, "membership_id": row.id},
        )
        await session.commit()
    # ADR §11: invalidate the user's authz cache so the revocation is
    # observed on the next request (and any in-flight SSE re-validation),
    # not up to the ≤60s TTL. Post-commit: a rolled-back transition (outbox
    # write failure) never reaches here.
    get_authorize_service().invalidate_principal(org_id=org_id, principal_type="user", principal_id=user_id)
    return _to_membership_response(row)


@router.post(
    "/org-memberships/{user_id}:activate",
    response_model=OrgMembershipResponse,
)
@require_rbac(Permission.ADMIN_IAM_MANAGE)
async def activate_org_member(request: Request, user_id: str) -> OrgMembershipResponse:
    """Lifecycle: suspended → active. Restores authorization."""
    org_id = _require_org_id(request)
    sf = _sf(request)
    # Class A same-transaction write (ADR §7.1).
    async with sf() as session:
        try:
            row = await set_membership_status(sf, org_id=org_id, user_id=user_id, status=MEMBERSHIP_ACTIVE, session=session)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found.") from exc
        await _emit_class_a_audit(
            session,
            action="iam.membership.activated",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="org_membership", id_=row.id, org_id=org_id),
            payload={"user_id": user_id, "membership_id": row.id},
        )
        await session.commit()
    # Invalidate so the restored permissions are observed immediately.
    get_authorize_service().invalidate_principal(org_id=org_id, principal_type="user", principal_id=user_id)
    return _to_membership_response(row)


@router.get(
    "/org-memberships/{user_id}",
    response_model=OrgMembershipResponse,
)
@require_rbac(Permission.ADMIN_IAM_READ)
async def get_org_member(request: Request, user_id: str) -> OrgMembershipResponse:
    """Read one membership row (current status). Cross-Org → 404."""
    org_id = _require_org_id(request)
    row = await get_membership(_sf(request), org_id=org_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found.")
    return _to_membership_response(row)


# ---------------------------------------------------------------------------
# Dep injection
# ---------------------------------------------------------------------------


def _sf(request: Request):
    """Return the request's session factory.

    Tests seed the same factory (``rbac_sf``) before invoking the router
    via TestClient, so repository writes land in the same isolated
    SQLite the assertions read back. Production wires the factory on
    ``app.state.session_factory`` during lifespan; the fallback keeps
    the test path (which mounts the router on a bare FastAPI app
    without the full lifespan) working.
    """
    sf = getattr(request.app.state, "session_factory", None)
    if sf is not None:
        return sf
    from deerflow.persistence.engine import get_session_factory

    return get_session_factory()
