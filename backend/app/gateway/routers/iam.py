"""IAM ServiceAccount API (PR-034) + API Key endpoints (PR-035).

Thirteen endpoints mounted at ``/api/v1/iam``:

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

Audit: every mutation emits a ``service_account_*`` /
``service_account_role_binding_*`` / ``api_key_*`` event through the
``emit_tenant_event`` logger shim (PR-041 will replace the shim with
the real AuditEvent outbox — TODO marker in each call). Cache
invalidation runs after the commit via
:meth:`AuthorizeService.invalidate_principal` (ADR §11).

Cross-Org isolation (ADR §8 "列表与查询强制 Org 过滤"): every endpoint
takes the caller's ``org_id`` from the bound ``TenantContext`` and
returns 404 (not 403) for a SA that exists in another Org —
existence-hiding, matching the posture established in PR-031/032/033.

What this router deliberately does NOT do (PR boundary):

* It does not emit real AuditEvent outbox rows — PR-041.
* It does not implement rate limiting (ADR §9.3) — platform limiter PR.
* It does not emit a dedicated ``api_key_rotated`` event — rotation is
  the composition of ``api_key_created`` + ``api_key_revoked`` (≤24h).
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
    ServiceAccountCreateRequest,
    ServiceAccountResponse,
    ServiceAccountRoleBindingRequest,
    ServiceAccountRoleBindingResponse,
    ServiceAccountUpdateRequest,
)
from deerflow.contracts.rbac import SYSTEM_PERMISSION_PREFIX, validate_role_permissions
from deerflow.persistence.iam import (
    SERVICE_ACCOUNT_ACTIVE,
    SERVICE_ACCOUNT_DISABLED,
    create_api_key,
    create_role_binding,
    create_service_account,
    delete_role_binding,
    delete_service_account,
    get_api_key,
    get_service_account,
    list_api_keys,
    list_role_bindings,
    list_service_accounts,
    revoke_api_key,
    set_service_account_status,
    update_service_account,
)
from deerflow.tenancy.audit_events import emit_tenant_event

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
    try:
        row = await create_service_account(
            _sf(request),
            org_id=org_id,
            name=body.name,
            description=body.description,
            owner_user_id=body.owner_user_id,
            purpose=body.purpose,
            system=body.system,
            environment=body.environment,
            expires_at=body.expires_at,
            created_by=actor,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"ServiceAccount named {body.name!r} already exists in this organization.",
        ) from exc
    # TODO(PR-041): replace emit_tenant_event with a real AuditEvent outbox write.
    emit_tenant_event(
        "service_account_created",
        org_id=org_id,
        principal_id=actor,
        payload={"sa_id": row.id, "name": row.name, "owner_user_id": row.owner_user_id},
    )
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
    actor = _actor_id(request)
    fields = body.model_dump(exclude_unset=True)
    try:
        row = await update_service_account(_sf(request), service_account_id=sa_id, **fields)
    except ValueError as exc:
        # Either an unknown field (defensive — pydantic extra=forbid catches
        # this earlier) or the row is missing. Treat both as 404 for
        # existence-hiding.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.") from exc
    if row.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    # TODO(PR-041): replace emit_tenant_event with a real AuditEvent outbox write.
    emit_tenant_event(
        "service_account_updated",
        org_id=org_id,
        principal_id=actor,
        payload={"sa_id": row.id, "fields": sorted(fields)},
    )
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
    actor = _actor_id(request)
    try:
        row = await set_service_account_status(_sf(request), service_account_id=sa_id, status=target)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.") from exc
    if row.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    # TODO(PR-041): replace emit_tenant_event with a real AuditEvent outbox write.
    emit_tenant_event(
        f"service_account_{target}",
        org_id=org_id,
        principal_id=actor,
        payload={"sa_id": row.id, "status": target},
    )
    # The SA's own cache entry may be live; drop it so the next auth
    # attempt picks up the new status (ADR §11 SLO ≤60s, active
    # invalidation is the preferred path).
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
    actor = _actor_id(request)
    existing = await get_service_account(_sf(request), service_account_id=sa_id)
    if existing is None or existing.org_id != org_id:
        # Existence-hiding: identical 404 for "missing" and "wrong Org".
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    # Emit the audit event BEFORE the delete — the row carries ``name`` /
    # ``id`` we want in the audit payload, and once it is gone we cannot
    # recover them. TODO(PR-041): real outbox write in the same transaction.
    emit_tenant_event(
        "service_account_deleted",
        org_id=org_id,
        principal_id=actor,
        payload={"sa_id": existing.id, "name": existing.name},
    )
    await delete_service_account(_sf(request), service_account_id=sa_id)
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
    sa = await get_service_account(_sf(request), service_account_id=sa_id)
    if sa is None or sa.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    try:
        row = await create_role_binding(
            _sf(request),
            org_id=org_id,
            principal_type="service_account",
            principal_id=sa_id,
            role_id=body.role_id,
            created_by=actor,
            expires_at=body.expires_at,
        )
    except IntegrityError as exc:
        # Either the (org, principal, role) tuple already exists, or
        # role_id does not point at a real roles row (FK violation).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role binding already exists or role_id is invalid.",
        ) from exc
    # TODO(PR-041): replace emit_tenant_event with a real AuditEvent outbox write.
    emit_tenant_event(
        "service_account_role_binding_created",
        org_id=org_id,
        principal_id=actor,
        payload={"sa_id": sa_id, "binding_id": row.id, "role_id": body.role_id},
    )
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
    actor = _actor_id(request)
    sa = await get_service_account(_sf(request), service_account_id=sa_id)
    if sa is None or sa.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    # TODO(PR-041): replace emit_tenant_event with a real AuditEvent outbox write.
    emit_tenant_event(
        "service_account_role_binding_deleted",
        org_id=org_id,
        principal_id=actor,
        payload={"sa_id": sa_id, "binding_id": binding_id},
    )
    await delete_role_binding(_sf(request), binding_id=binding_id, org_id=org_id)
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
    actor = _actor_id(request)
    sa = await get_service_account(_sf(request), service_account_id=sa_id)
    if sa is None or sa.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    _validate_scopes(body.scopes)

    # Mint + insert. Retry once on a prefix collision (2^48 space,
    # collision probability is negligible; the retry keeps the user-facing
    # 409 for a *repeated* collision which would indicate a real bug).
    plaintext, key_prefix, key_hash = generate_api_key()
    try:
        row = await create_api_key(
            _sf(request),
            org_id=org_id,
            service_account_id=sa_id,
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=body.scopes,
            expires_at=body.expires_at,
        )
    except IntegrityError:
        plaintext, key_prefix, key_hash = generate_api_key()
        try:
            row = await create_api_key(
                _sf(request),
                org_id=org_id,
                service_account_id=sa_id,
                key_prefix=key_prefix,
                key_hash=key_hash,
                scopes=body.scopes,
                expires_at=body.expires_at,
            )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="API Key prefix collision after retry; please retry the request.",
            ) from exc

    # TODO(PR-041): replace emit_tenant_event with a real AuditEvent outbox write.
    # The payload MUST NOT contain the plaintext or the hash (ADR §9.2 line 302).
    emit_tenant_event(
        "api_key_created",
        org_id=org_id,
        principal_id=actor,
        payload={
            "key_id": row.id,
            "key_prefix": row.key_prefix,
            "sa_id": sa_id,
            "scopes": list(body.scopes),
            "expires_at": body.expires_at.isoformat(),
        },
    )
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
    actor = _actor_id(request)
    sa = await get_service_account(_sf(request), service_account_id=sa_id)
    if sa is None or sa.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ServiceAccount not found.")
    # Confirm the key belongs to this SA in this Org before revoking —
    # otherwise a caller could revoke a foreign-Org key by guessing the id.
    key = await get_api_key(_sf(request), api_key_id=key_id)
    if key is None or key.org_id != org_id or key.service_account_id != sa_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API Key not found.")
    # TODO(PR-041): replace emit_tenant_event with a real AuditEvent outbox write.
    emit_tenant_event(
        "api_key_revoked",
        org_id=org_id,
        principal_id=actor,
        payload={"key_id": key_id, "key_prefix": key.key_prefix, "sa_id": sa_id},
    )
    await revoke_api_key(_sf(request), api_key_id=key_id, org_id=org_id)
    # Defensive invalidation (no-op per cache-walk-through, but matches
    # ADR §11 "API Key ... 变更主动失效" wording letter-for-letter).
    get_authorize_service().invalidate_principal(org_id=org_id, principal_type="service_account", principal_id=sa_id)


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
