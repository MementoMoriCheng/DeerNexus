"""AgentPackage / AgentVersion HTTP API (PR-052).

Mounted at ``/api/v1``. Mirrors the IAM router (PR-034) for the Class A
write skeleton: ``async with sf() as session:`` → repository write (session
passthrough) → ``_emit_class_a_audit`` (same-transaction outbox enqueue) →
``session.commit()`` (atomic business write + audit row). RBAC gating uses
``STUDIO_PACKAGE_READ`` (reads) / ``STUDIO_PACKAGE_WRITE`` (writes), both
carried only by ``org:admin`` (developer/viewer get 403 via the decorator).

ADR-0004 §3/§4/§11 + ADR-0005 §7.1. Version lifecycle transitions
(``:review`` / ``:publish`` / ``:revoke``) go through dedicated endpoints
(Google-AIP verbs) so the call site is explicit about which side of the §4
state machine it exercises. ``:publish`` stamps ``published_at`` and freezes
content; the repository enforces the published-immutability invariant.

This PR delivers the studio write path + the inventory (reconciliation)
read endpoint. It does NOT deliver channel promote/rollback (PR-053/055) or
ReleaseRef resolution (PR-054) — ``:publish`` here only moves the Version
into the published state; channel promotion is a separate CAS operation.
PR-051 adds ``POST /agent-packages:import-file`` for the file-state →
artifact import flow (ADR §10): ``config.yaml`` + ``SOUL.md`` → ``Manifest``
→ digest → draft Version, idempotent on digest.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app.gateway.rbac import require_rbac
from deerflow.config.app_config import get_app_config
from deerflow.contracts import Permission, get_tenant_context
from deerflow.contracts.agent_artifact import (
    AgentPackageCreateRequest,
    AgentPackageResponse,
    AgentPackageUpdateRequest,
    AgentVersionCreateRequest,
    AgentVersionResponse,
    ImportFileRequest,
    ImportReport,
)
from deerflow.contracts.identity import PrincipalRef
from deerflow.contracts.policy import ResourceRef
from deerflow.persistence.audit import enqueue_audit_outbox_in_session
from deerflow.persistence.release import (
    PACKAGE_ARCHIVED,
    VERSION_PUBLISHED,
    VERSION_REVIEWED,
    VERSION_REVOKED,
    archive_agent_package,
    create_agent_package,
    create_agent_version,
    get_agent_package,
    get_agent_version,
    list_agent_packages,
    list_agent_versions,
    reconcile_versions,
    set_version_status,
    update_agent_package,
)
from deerflow.persistence.release.importer import (
    ArtifactTooLargeError,
    ImportPathError,
    import_agent_from_file,
)
from deerflow.persistence.release.repository import (
    IllegalVersionTransitionError,
)
from deerflow.tenancy.audit_events import build_audit_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["agent-artifacts"])


# ---------------------------------------------------------------------------
# request helpers (mirror iam.py)
# ---------------------------------------------------------------------------


def _require_org_id(request: Request) -> str:
    """Resolve the caller's active ``org_id`` from the bound TenantContext.

    Raises 400 (not 403) when no tenant is bound — artifact ops are per-Org
    and an anonymous request has no business here (mirrors the IAM gate).
    """
    ctx = get_tenant_context()
    org_id = getattr(ctx, "org_id", None) if ctx else None
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No active organization context.")
    return org_id


def _actor_id(request: Request) -> str | None:
    user = getattr(request.state, "user", None)
    if user is None:
        return None
    return str(getattr(user, "id", None)) if getattr(user, "id", None) is not None else None


def _audit_actor(request: Request) -> PrincipalRef:
    """Build the audit ``PrincipalRef`` for the authenticated caller."""
    user_id = _actor_id(request)
    if user_id is not None:
        return PrincipalRef(type="user", id=user_id, user_id=user_id)
    return PrincipalRef(type="system", id="system")


def _audit_resource(*, type_: str, id_: str | None, org_id: str) -> ResourceRef:
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
    """Same-transaction Class A audit enqueue (ADR-0005 §7.1)."""
    event = build_audit_event(
        action,
        org_id=org_id,
        actor=actor,
        outcome="success",
        resource=resource,
        payload=payload or {},
    )
    await enqueue_audit_outbox_in_session(session, event)


def _sf(request: Request):
    """Return the request's session factory (mirrors iam.py)."""
    sf = getattr(request.app.state, "session_factory", None)
    if sf is not None:
        return sf
    from deerflow.persistence.engine import get_session_factory

    return get_session_factory()


def _inline_threshold(request: Request) -> int:
    """Read the configured inline size threshold (ADR-0004 §11.1).

    Falls back to the repository default if the app config is unavailable
    (the dev/test path may not load a ``config.yaml``; production always
    has one). Mirrors the fail-soft pattern other routers use for optional
    config so a missing config file never 500s an artifact create.
    """
    try:
        config = get_app_config()
        return config.production.artifact.inline_size_threshold
    except FileNotFoundError:
        from deerflow.persistence.release.repository import _DEFAULT_INLINE_THRESHOLD

        return _DEFAULT_INLINE_THRESHOLD


def _pkg_response(row) -> AgentPackageResponse:
    return AgentPackageResponse.model_validate(row)


def _ver_response(row) -> AgentVersionResponse:
    return AgentVersionResponse.model_validate(row)


# ---------------------------------------------------------------------------
# AgentPackage endpoints
# ---------------------------------------------------------------------------


@router.get("/agent-packages", response_model=list[AgentPackageResponse])
@require_rbac(Permission.STUDIO_PACKAGE_READ)
async def list_packages(request: Request) -> list[AgentPackageResponse]:
    """List packages in the caller's Org (ADR §8 Org filter). Archived excluded."""
    org_id = _require_org_id(request)
    rows = await list_agent_packages(_sf(request), org_id=org_id)
    return [_pkg_response(r) for r in rows]


@router.post("/agent-packages", response_model=AgentPackageResponse, status_code=status.HTTP_201_CREATED)
@require_rbac(Permission.STUDIO_PACKAGE_WRITE)
async def create_package(request: Request, body: AgentPackageCreateRequest) -> AgentPackageResponse:
    """Create a package (ADR §3.1). ``(org_id, name)`` collision → 409."""
    org_id = _require_org_id(request)
    sf = _sf(request)
    try:
        async with sf() as session:
            row = await create_agent_package(
                sf,
                org_id=org_id,
                name=body.name,
                display_name=body.display_name,
                description=body.description,
                workspace_id=body.workspace_id,
                created_by=_actor_id(request),
                session=session,
            )
            await _emit_class_a_audit(
                session,
                action="catalog.agent_package.created",
                org_id=org_id,
                actor=_audit_actor(request),
                resource=_audit_resource(type_="agent_package", id_=row.id, org_id=org_id),
                payload={"package_id": row.id, "name": row.name},
            )
            await session.commit()
    except IntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Package name already exists in this Org.") from exc
    return _pkg_response(row)


@router.get("/agent-packages/{package_id}", response_model=AgentPackageResponse)
@require_rbac(Permission.STUDIO_PACKAGE_READ)
async def get_package(request: Request, package_id: str) -> AgentPackageResponse:
    """Get one package. Cross-Org miss → 404 (existence-hiding)."""
    org_id = _require_org_id(request)
    row = await get_agent_package(_sf(request), package_id=package_id, org_id=org_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found.")
    return _pkg_response(row)


@router.patch("/agent-packages/{package_id}", response_model=AgentPackageResponse)
@require_rbac(Permission.STUDIO_PACKAGE_WRITE)
async def patch_package(request: Request, package_id: str, body: AgentPackageUpdateRequest) -> AgentPackageResponse:
    """PATCH mutable display fields. Identity (name/org/workspace) immutable. Miss → 404."""
    org_id = _require_org_id(request)
    sf = _sf(request)
    async with sf() as session:
        row = await update_agent_package(
            sf,
            package_id=package_id,
            org_id=org_id,
            display_name=body.display_name,
            description=body.description,
            session=session,
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found.")
        await _emit_class_a_audit(
            session,
            action="catalog.agent_package.updated",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="agent_package", id_=row.id, org_id=org_id),
            payload={"package_id": row.id},
        )
        await session.commit()
    return _pkg_response(row)


@router.post("/agent-packages/{package_id}:archive", response_model=AgentPackageResponse)
@require_rbac(Permission.STUDIO_PACKAGE_WRITE)
async def archive_package(request: Request, package_id: str) -> AgentPackageResponse:
    """Archive a package (soft-delete, ADR §3.1/§11.3). Miss → 404."""
    org_id = _require_org_id(request)
    sf = _sf(request)
    async with sf() as session:
        row = await archive_agent_package(sf, package_id=package_id, org_id=org_id, session=session)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found.")
        await _emit_class_a_audit(
            session,
            action="catalog.agent_package.archived",
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="agent_package", id_=row.id, org_id=org_id),
            payload={"package_id": row.id, "status": PACKAGE_ARCHIVED},
        )
        await session.commit()
    return _pkg_response(row)


# ---------------------------------------------------------------------------
# AgentVersion endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/agent-packages/{package_id}/versions",
    response_model=list[AgentVersionResponse],
)
@require_rbac(Permission.STUDIO_PACKAGE_READ)
async def list_versions(request: Request, package_id: str) -> list[AgentVersionResponse]:
    """List versions of a package in the caller's Org."""
    org_id = _require_org_id(request)
    # Verify the package belongs to the caller's Org before listing its
    # versions (defense-in-depth: the version rows also carry org_id, but a
    # package-id-only request should not reveal versions of a foreign package).
    pkg = await get_agent_package(_sf(request), package_id=package_id, org_id=org_id)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found.")
    rows = await list_agent_versions(_sf(request), org_id=org_id, package_id=package_id)
    return [_ver_response(r) for r in rows]


@router.post(
    "/agent-packages/{package_id}/versions",
    response_model=AgentVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
@require_rbac(Permission.STUDIO_PACKAGE_WRITE)
async def create_version(request: Request, package_id: str, body: AgentVersionCreateRequest) -> AgentVersionResponse:
    """Create a draft version (ADR §3.2/§11).

    Computes ``digest`` (``sha256:<hex>``) from ``content`` and routes storage
    inline / object_key per the production threshold. ``content`` is never
    echoed back in the response (the envelope carries only digest + size +
    the opaque object_key). Collisions on ``(org, package, version)`` or
    ``(org, digest)`` → 409.
    """
    org_id = _require_org_id(request)
    sf = _sf(request)
    pkg = await get_agent_package(sf, package_id=package_id, org_id=org_id)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found.")
    threshold = _inline_threshold(request)
    try:
        async with sf() as session:
            row = await create_agent_version(
                sf,
                org_id=org_id,
                package_id=package_id,
                version=body.version,
                manifest=body.manifest.model_dump(),
                content=body.content,
                workspace_id=pkg.workspace_id,
                created_by=_actor_id(request),
                inline_size_threshold=threshold,
                session=session,
            )
            await _emit_class_a_audit(
                session,
                action="catalog.agent_version.created",
                org_id=org_id,
                actor=_audit_actor(request),
                resource=_audit_resource(type_="agent_version", id_=row.id, org_id=org_id),
                payload={"version_id": row.id, "package_id": package_id, "version": row.version, "digest": row.digest},
            )
            await session.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Version or digest already exists for this package in this Org.",
        ) from exc
    return _ver_response(row)


@router.get("/agent-versions/{version_id}", response_model=AgentVersionResponse)
@require_rbac(Permission.STUDIO_PACKAGE_READ)
async def get_version(request: Request, version_id: str) -> AgentVersionResponse:
    """Get one version. Cross-Org miss → 404."""
    org_id = _require_org_id(request)
    row = await get_agent_version(_sf(request), version_id=version_id, org_id=org_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")
    return _ver_response(row)


async def _transition_version(
    request: Request,
    version_id: str,
    target: str,
    action: str,
    payload_extra: dict | None = None,
) -> AgentVersionResponse:
    """Shared skeleton for ``:review`` / ``:publish`` / ``:revoke`` (ADR §4).

    Runs the status transition + the Class A audit in one transaction.
    Illegal transitions (``IllegalVersionTransitionError``) → 409; miss → 404.
    """
    org_id = _require_org_id(request)
    sf = _sf(request)
    async with sf() as session:
        try:
            row = await set_version_status(sf, version_id=version_id, org_id=org_id, status=target, session=session)
        except IllegalVersionTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.") from exc
        payload = {"version_id": row.id, "package_id": row.package_id, "status": target}
        if payload_extra:
            payload.update(payload_extra)
        await _emit_class_a_audit(
            session,
            action=action,
            org_id=org_id,
            actor=_audit_actor(request),
            resource=_audit_resource(type_="agent_version", id_=row.id, org_id=org_id),
            payload=payload,
        )
        await session.commit()
    return _ver_response(row)


@router.post("/agent-versions/{version_id}:review", response_model=AgentVersionResponse)
@require_rbac(Permission.STUDIO_PACKAGE_WRITE)
async def review_version(request: Request, version_id: str) -> AgentVersionResponse:
    """Move a draft → reviewed (ADR §4.2)."""
    return await _transition_version(request, version_id, VERSION_REVIEWED, "catalog.agent_version.reviewed")


@router.post("/agent-versions/{version_id}:publish", response_model=AgentVersionResponse)
@require_rbac(Permission.STUDIO_PACKAGE_WRITE)
async def publish_version(request: Request, version_id: str) -> AgentVersionResponse:
    """Publish a version (ADR §4.3): freezes content, stamps ``published_at``.

    Note: this moves the Version into the published state and emits
    ``catalog.agent_version.published``. It is distinct from a Channel
    promote (``release.agent.published``, PR-053) — promoting to a channel
    is a separate CAS operation.
    """
    return await _transition_version(request, version_id, VERSION_PUBLISHED, "catalog.agent_version.published")


@router.post("/agent-versions/{version_id}:revoke", response_model=AgentVersionResponse)
@require_rbac(Permission.STUDIO_PACKAGE_WRITE)
async def revoke_version(request: Request, version_id: str) -> AgentVersionResponse:
    """Revoke a version (ADR §4.4): stamps ``revoked_at``, blocks new Runs (PR-054)."""
    return await _transition_version(request, version_id, VERSION_REVOKED, "catalog.agent_version.revoked")


# ---------------------------------------------------------------------------
# Inventory / reconciliation (ADR §11.2)
# ---------------------------------------------------------------------------


@router.post("/agent-packages:reconcile")
@require_rbac(Permission.STUDIO_PACKAGE_READ)
async def reconcile_inventory(request: Request) -> dict:
    """Reconcile object-backed versions against the ObjectStore (ADR §11.2).

    Returns a report of missing/mismatched object references. With the MVP
    inline backend this is always clean (an inline artifact cannot be missing
    from its own row); the endpoint exists so a future S3 backend + doctor
    probe consume a real reconciliation.
    """
    org_id = _require_org_id(request)
    report = await reconcile_versions(_sf(request), org_id=org_id)
    return {
        "org_id": report.org_id,
        "checked_count": report.checked_count,
        "is_clean": report.is_clean,
        "missing_versions": [vars(m) for m in report.missing_versions],
    }


# ---------------------------------------------------------------------------
# File-state import (PR-051, ADR-0004 §10)
# ---------------------------------------------------------------------------


def _import_report(pkg_row, ver_row, digest: str, imported: bool, source_metadata: dict) -> ImportReport:
    """Build the response envelope off the row pair returned by the importer."""
    return ImportReport(
        package=AgentPackageResponse.model_validate(pkg_row),
        version=AgentVersionResponse.model_validate(ver_row),
        digest=digest,
        imported=imported,
        source_metadata=source_metadata,
    )


@router.post("/agent-packages:import-file", response_model=ImportReport)
@require_rbac(Permission.STUDIO_PACKAGE_WRITE)
async def import_file(request: Request, body: ImportFileRequest) -> ImportReport:
    """Import one file-state agent into the artifact store (ADR-0004 §10).

    Reads ``{base_dir}/users/{user_id?}/agents/{name}/`` (or the legacy shared
    layout), projects ``config.yaml`` + ``SOUL.md`` into a ``Manifest``,
    computes the canonical-JSON digest, and creates a draft ``AgentVersion``
    (plus the parent ``AgentPackage`` if absent). Idempotent on digest: a
    re-import of identical content returns the existing Version with
    ``imported=False`` instead of failing (ADR §10 "重复 digest 导入幂等").

    The Catalog index entry (ADR §10 step 6) is deferred to PR-054; provenance
    lives in ``Manifest.source_metadata`` until the discovery table lands.

    Error mapping:

    * 400 ``import_path_unsafe`` — path traversal / symlink refused (ADR §9.1)
    * 404 — agent directory absent (existence-hiding: identical to a missing
      Package/Version 404)
    * 409 — ``(org, package, version)`` collision (content changed but caller
      did not bump ``version``)
    * 413 — source file exceeds the 1 MiB cap
    * 422 — Pydantic validation (bad SemVer, etc.) — handled by FastAPI
    """
    org_id = _require_org_id(request)
    sf = _sf(request)
    actor = _audit_actor(request)
    actor_id = _actor_id(request)
    threshold = _inline_threshold(request)
    try:
        async with sf() as session:
            pkg_row, ver_row, digest, imported, source_metadata = await import_agent_from_file(
                sf,
                org_id=org_id,
                name=body.name,
                version=body.version,
                user_id=body.user_id,
                display_name=body.display_name,
                description=body.description,
                workspace_id=body.workspace_id,
                created_by=actor_id,
                inline_size_threshold=threshold,
                session=session,
            )
            # Always record the package-side import attempt; emit the version
            # action only when a new Version was actually created (idempotent
            # re-imports don't get a second version row).
            await _emit_class_a_audit(
                session,
                action="catalog.agent_package.imported",
                org_id=org_id,
                actor=actor,
                resource=_audit_resource(type_="agent_package", id_=pkg_row.id, org_id=org_id),
                payload={
                    "package_id": pkg_row.id,
                    "name": body.name,
                    "version": body.version,
                    "imported": imported,
                    "digest": digest,
                },
            )
            if imported:
                await _emit_class_a_audit(
                    session,
                    action="catalog.agent_version.imported",
                    org_id=org_id,
                    actor=actor,
                    resource=_audit_resource(type_="agent_version", id_=ver_row.id, org_id=org_id),
                    payload={
                        "version_id": ver_row.id,
                        "package_id": pkg_row.id,
                        "version": ver_row.version,
                        "digest": digest,
                        "source": source_metadata.get("source"),
                    },
                )
            await session.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Version already exists for this package in this Org; bump the version if content changed.",
        ) from exc
    except ArtifactTooLargeError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc
    except ImportPathError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.") from exc
    return _import_report(pkg_row, ver_row, digest, imported, source_metadata)
