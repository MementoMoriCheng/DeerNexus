"""Catalog discovery-index HTTP API (PR-054).

Mounted at ``/api/v1``. ``GET /orgs/{org_id}/catalog`` lists the
``catalog_entries`` discovery rows for the caller's Org (data-model.md §6.6,
api-boundaries.md §344). RBAC gating uses ``STUDIO_PACKAGE_READ`` (admin-only
per the PR-030 registry pin — developer/viewer get 403 via the decorator).

The ``org_id`` path parameter is **defensive** — it MUST equal the caller's
bound TenantContext.org_id; a mismatch → 403 (cross-Org probing). The actual
filter uses the tenant's org_id, never the raw path value, so a forged path
cannot read another Org's catalog.

The Catalog is a discovery index, not execution authority (ADR §10). The
write path (import / promote projecting into ``catalog_entries``) lands in a
follow-up; this PR ships the read endpoint only, so it returns ``[]`` until
the writer is wired.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.gateway.rbac import require_rbac
from deerflow.contracts import Permission, get_tenant_context
from deerflow.contracts.catalog import CatalogEntryResponse
from deerflow.persistence.catalog import list_catalog_entries

router = APIRouter(prefix="/api/v1", tags=["catalog"])


def _require_org_id(request: Request) -> str:
    """Resolve the caller's active ``org_id`` from the bound TenantContext.

    Raises 400 (not 403) when no tenant is bound — catalog ops are per-Org
    and an anonymous request has no business here (mirrors the artifact gate).
    """
    ctx = get_tenant_context()
    org_id = getattr(ctx, "org_id", None) if ctx else None
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No active organization context.")
    return org_id


def _sf(request: Request):
    """Return the request's session factory (mirrors agent_artifacts.py)."""
    sf = getattr(request.app.state, "session_factory", None)
    if sf is not None:
        return sf
    from deerflow.persistence.engine import get_session_factory

    return get_session_factory()


@router.get("/orgs/{org_id}/catalog", response_model=list[CatalogEntryResponse])
@require_rbac(Permission.STUDIO_PACKAGE_READ)
async def list_catalog(
    request: Request,
    org_id: str,
    resource_type: str | None = Query(default=None, description="Filter by resource type (agent/skill/mcp/tool)."),
    workspace_id: str | None = Query(default=None, description="Filter by workspace."),
) -> list[CatalogEntryResponse]:
    """List catalog entries in the caller's Org (ADR §8 Org filter).

    Defaults to ``status='active'`` only (archived / disabled hidden from the
    default browse). Optional ``resource_type`` and ``workspace_id`` filters.
    The ``org_id`` path param must equal the bound tenant's org_id (else 403).
    """
    tenant_org_id = _require_org_id(request)
    if org_id != tenant_org_id:
        # Cross-Org probing — existence-hide as 403 (the caller is authed but
        # asking for an Org they are not bound to).
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Catalog path org_id does not match the active organization context.",
        )
    rows = await list_catalog_entries(
        _sf(request),
        org_id=tenant_org_id,
        workspace_id=workspace_id,
        resource_type=resource_type,
    )
    return [CatalogEntryResponse.model_validate(r) for r in rows]
