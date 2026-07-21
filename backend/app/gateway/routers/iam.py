"""IAM ServiceAccount API (PR-034).

Ten endpoints for ServiceAccount lifecycle and role bindings, mounted at
``/api/v1/iam``:

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

Audit: every mutation emits a ``service_account_*`` /
``service_account_role_binding_*`` event through the ``emit_tenant_event``
logger shim (PR-041 will replace the shim with the real AuditEvent
outbox — TODO marker in each call). Cache invalidation runs after the
commit via :meth:`AuthorizeService.invalidate_principal` (ADR §11).

Cross-Org isolation (ADR §8 "列表与查询强制 Org 过滤"): every endpoint
takes the caller's ``org_id`` from the bound ``TenantContext`` and
returns 404 (not 403) for a SA that exists in another Org —
existence-hiding, matching the posture established in PR-031/032/033.

What this router deliberately does NOT do (PR boundary):

* It does not implement API Key mint/rotate/revoke — PR-035. Today a
  ServiceAccount can be created and granted roles but cannot
  authenticate via an API key; the ``authorize()`` service_account
  branch is exercised through service-layer tests that construct a
  ``PrincipalRef(type="service_account", ...)`` directly.
* It does not emit real AuditEvent outbox rows — PR-041.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app.gateway.authorize import get_authorize_service
from app.gateway.rbac import require_rbac
from deerflow.contracts import Permission, get_tenant_context
from deerflow.contracts.iam import (
    ServiceAccountCreateRequest,
    ServiceAccountResponse,
    ServiceAccountRoleBindingRequest,
    ServiceAccountRoleBindingResponse,
    ServiceAccountUpdateRequest,
)
from deerflow.persistence.iam import (
    SERVICE_ACCOUNT_ACTIVE,
    SERVICE_ACCOUNT_DISABLED,
    create_role_binding,
    create_service_account,
    delete_role_binding,
    delete_service_account,
    get_service_account,
    list_role_bindings,
    list_service_accounts,
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
