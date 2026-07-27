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
PR-053 adds the channel layer (ADR §5/§7/§8): ``:promote`` / ``:rollback``
CAS on ``release_channels`` + ``GET`` channel/event reads. Promote uses a
dynamic permission gate — dev accepts ``studio:release:promote_dev`` OR
``studio:release:promote`` (so developers can move dev), staging/prod
require ``studio:release:promote`` (admin-only); rollback requires
``studio:release:rollback`` (admin-only).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError

from app.gateway.errors import contract_error_response as _shared_contract_error_response
from app.gateway.errors import request_id as _shared_request_id
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
    PromoteRequest,
    PromoteResponse,
    ReleaseChannelResponse,
    ReleaseEventResponse,
    RollbackRequest,
)
from deerflow.contracts.errors import ErrorCode
from deerflow.contracts.identity import PrincipalRef
from deerflow.contracts.policy import ResourceRef
from deerflow.persistence.audit import enqueue_audit_outbox_in_session
from deerflow.persistence.release import (
    CHANNEL_DEV,
    CHANNEL_PROD,
    CHANNEL_STAGING,
    EVENT_ACTION_PROMOTE,
    EVENT_ACTION_ROLLBACK,
    IDEMPOTENCY_KEY_HEADER,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    PACKAGE_ARCHIVED,
    VERSION_PUBLISHED,
    VERSION_REVIEWED,
    VERSION_REVOKED,
    ChannelGateError,
    IdempotencyConflictError,
    ReleaseConflictError,
    archive_agent_package,
    compute_request_hash,
    create_agent_package,
    create_agent_version,
    get_agent_package,
    get_agent_version,
    get_channel,
    get_idempotency_record,
    insert_idempotency_record,
    list_agent_packages,
    list_agent_versions,
    list_channels,
    list_events,
    promote_channel,
    reconcile_versions,
    resolve_idempotency_outcome,
    rollback_channel,
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


# ---------------------------------------------------------------------------
# Release channels (PR-053, ADR-0004 §5/§7/§8)
# ---------------------------------------------------------------------------

_ALLOWED_CHANNEL_VALUES = {CHANNEL_DEV, CHANNEL_STAGING, CHANNEL_PROD}


def _validate_channel(channel: str) -> str:
    """Reject unknown channel path params with 404 (existence-hiding).

    A bad channel value is not a 422 (the path schema accepts any string);
    treating it as 404 avoids leaking the closed set to an unauthorised
    caller and matches the cross-Org miss convention.
    """
    if channel not in _ALLOWED_CHANNEL_VALUES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found.")
    return channel


async def _require_promote_permission(request: Request, channel: str) -> None:
    """Dynamic promote permission gate (ADR §14).

    dev channel accepts ``studio:release:promote_dev`` OR
    ``studio:release:promote`` (admin has both; developer has only
    ``promote_dev``). staging / prod require ``studio:release:promote``
    (admin-only). The decorator baseline is ``promote_dev`` (the looser
    permission) so devs reach this handler for dev; non-dev channels are
    re-checked here for the stricter ``promote``.

    Honours the test-bypass flag (``_deerflow_test_bypass_auth``) the same way
    ``@require_rbac`` does, so business-path tests with
    ``make_rbac_test_app(bypass_authorize=True)`` skip the in-handler
    re-authorize as well.
    """
    from app.gateway.authorize import AuthorizeError, get_authorize_service
    from app.gateway.rbac import _request_has_bypass_flag

    if _request_has_bypass_flag(request):
        return  # business-path test bypass — decorator already short-circuited
    ctx = get_tenant_context()
    if ctx is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    svc = get_authorize_service()
    if channel == CHANNEL_DEV:
        # dev: accept either. Try the stricter first (admin fast-path), then
        # fall back to promote_dev (developer path). Either allow short-circuits.
        for perm in (Permission.STUDIO_RELEASE_PROMOTE, Permission.STUDIO_RELEASE_PROMOTE_DEV):
            try:
                await svc.authorize(ctx, perm)
                return  # allowed
            except AuthorizeError:
                continue
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="promote to dev requires studio:release:promote_dev or studio:release:promote.",
        )
    # staging / prod: strict promote. The decorator already enforced
    # promote_dev (insufficient here), so re-authorize promote.
    try:
        await svc.authorize(ctx, Permission.STUDIO_RELEASE_PROMOTE)
    except AuthorizeError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"promote to {channel} requires studio:release:promote.",
        ) from None


def _channel_response(row) -> ReleaseChannelResponse:
    return ReleaseChannelResponse.model_validate(row)


def _event_response(row) -> ReleaseEventResponse:
    return ReleaseEventResponse.model_validate(row)


# ---------------------------------------------------------------------------
# PR-055: If-Match / ETag / Idempotency-Key helpers (ADR §7/§8)
# ---------------------------------------------------------------------------


def _request_id(request: Request) -> str:
    """Correlation id of the originating request (never empty).

    Thin wrapper over the shared :func:`app.gateway.errors.request_id` helper
    (extracted so the runs router can reuse it without importing the whole
    release-domain import list of this module).
    """
    return _shared_request_id(request)


def _contract_error_response(
    request: Request,
    code: ErrorCode,
    *,
    status_code: int,
    message: str = "",
    details: dict | None = None,
) -> HTTPException:
    """Build a uniform ``ContractError`` envelope as an ``HTTPException``.

    Thin wrapper over :func:`app.gateway.errors.contract_error_response`. The
    detail is the serialized ``ContractError`` dict, so a caller sees the same
    ``{code, message, retryable, request_id, details}`` shape across every
    promote/rollback failure. ``retryable`` is derived from the code by
    ``ContractError.from_code``.
    """
    return _shared_contract_error_response(
        request, code, status_code=status_code, message=message, details=details
    )


def _parse_if_match(request: Request) -> int | None:
    """Parse the ``If-Match`` header into the CAS ``expected_channel_version``.

    Accepts the canonical ``If-Match: "<row_version>"`` form (a quoted integer,
    matching the ``ETag`` we emit). Bare integers (``If-Match: 3``) are also
    accepted for ergonomics. Returns ``None`` when the header is absent. An
    unparseable value surfaces ``validation_error`` (not 412 — ADR §7 keeps
    the CAS miss on 409 ``release_conflict``; a malformed precondition is a
    client bug, surfaced as a 400 validation error instead).
    """
    raw = request.headers.get("if-match")
    if raw is None:
        return None
    value = raw.strip().strip('"').strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise _contract_error_response(
            request,
            ErrorCode.VALIDATION_ERROR,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=f"If-Match header not a quoted integer: {raw!r}",
        ) from exc
    if parsed < 1:
        raise _contract_error_response(
            request,
            ErrorCode.VALIDATION_ERROR,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=f"If-Match header must be >= 1, got {parsed}",
        )
    return parsed


def _resolve_expected_version(request: Request, body_expected: int | None) -> int:
    """Pick the CAS ``expected_channel_version`` (header precedence, ADR §7).

    ``If-Match`` wins when present; otherwise the body field is used. Exactly
    one MUST be present — the dual-track keeps PR-053's body contract working
    while letting callers that prefer headers (ADR §8 "使用 If-Match") opt in.
    """
    header_val = _parse_if_match(request)
    if header_val is not None:
        return header_val
    if body_expected is None:
        raise _contract_error_response(
            request,
            ErrorCode.VALIDATION_ERROR,
            status_code=status.HTTP_400_BAD_REQUEST,
            message="expected_channel_version is required: send it in the body or via the If-Match header.",
        )
    return body_expected


def _set_etag(response: Response, row_version: int) -> None:
    """Emit ``ETag: "<row_version>"`` so the caller echoes it on the next CAS."""
    response.headers["ETag"] = f'"{row_version}"'


def _read_idempotency_key(request: Request) -> str | None:
    """Read + validate the optional ``Idempotency-Key`` header (ADR §7).

    Returns ``None`` when absent (the request is non-idempotent — every call
    mutates). When present, the key is length-bounded (column width) and
    non-empty; violations surface ``validation_error``.
    """
    raw = request.headers.get(IDEMPOTENCY_KEY_HEADER.lower())
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        raise _contract_error_response(
            request,
            ErrorCode.VALIDATION_ERROR,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=f"{IDEMPOTENCY_KEY_HEADER} header must not be empty",
        )
    if len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise _contract_error_response(
            request,
            ErrorCode.VALIDATION_ERROR,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=f"{IDEMPOTENCY_KEY_HEADER} header exceeds {IDEMPOTENCY_KEY_MAX_LENGTH} chars",
        )
    return key


class _ReplayHit(Exception):
    """Internal control-flow signal: a replay returned a stored response.

    Carries the stored ``PromoteResponse`` (already materialized) and the
    original status code, so the handler can return it verbatim and skip the
    ``_move_channel`` + audit path entirely. Raised only inside
    :func:`_orchestrate_idempotent_move` when the happy-path read hits a
    matching record.
    """

    def __init__(self, *, response: PromoteResponse, status_code: int, etag: str | None) -> None:
        self.response = response
        self.status_code = status_code
        self.etag = etag
        super().__init__("idempotency replay hit")


async def _orchestrate_idempotent_move(
    request: Request,
    *,
    action: str,
    org_id: str,
    package_id: str,
    channel: str,
    body_target_version_id: str,
    body_expected_channel_version: int | None,
    body_workspace_id: str | None,
    body_reason: str | None,
    actor_id: str | None,
    actor: PrincipalRef,
    move_fn,  # promote_channel | rollback_channel
    audit_action: str,  # "release.agent.published" | "release.agent.rolled_back"
    response: Response,
) -> PromoteResponse:
    """Run a promote/rollback with optional Idempotency-Key replay (ADR §7).

    Pipeline:

    1. Resolve ``expected_channel_version`` (If-Match header precedence).
    2. If ``Idempotency-Key`` is present, read the replay record on the same
       session **before** any mutation:
       * same ``request_hash`` → raise :class:`_ReplayHit` (caller returns the
         stored response; ``_move_channel`` + audit are skipped);
       * different ``request_hash`` → raise :class:`IdempotencyConflictError`
         (caller → 409 ``idempotency_conflict``).
    3. Run ``move_fn`` (the CAS promote/rollback) + Class A audit, then
       ``insert_idempotency_record`` (same session) — all atomic.
    4. On ``IntegrityError`` (a concurrent same-key writer committed first):
       re-read on a fresh session, then replay or conflict per the winner's
       stored request_hash.

    When ``Idempotency-Key`` is absent, step 2 and 4 collapse and this is just
    the PR-053 path with ETag/If-Match layered on.
    """
    sf = _sf(request)
    expected_channel_version = _resolve_expected_version(request, body_expected_channel_version)
    idem_key = _read_idempotency_key(request)
    request_hash = compute_request_hash(
        action=action,
        package_id=package_id,
        channel=channel,
        target_version_id=body_target_version_id,
        workspace_id=body_workspace_id,
        reason=body_reason,
    )

    try:
        async with sf() as session:
            # Happy-path replay read (same transaction as the move so the read
            # sees nothing uncommitted from a racing writer).
            if idem_key is not None:
                existing = await get_idempotency_record(
                    session,
                    org_id=org_id,
                    idempotency_key=idem_key,
                )
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflictError(org_id=org_id, idempotency_key=idem_key)
                    replayed = PromoteResponse.model_validate(existing.response_payload)
                    _set_etag(response, replayed.channel.row_version)
                    raise _ReplayHit(
                        response=replayed,
                        status_code=existing.status_code,
                        etag=response.headers.get("ETag"),
                    )
            # No replay — perform the CAS move + Class A audit.
            ch_row, ev_row = await move_fn(
                sf,
                org_id=org_id,
                package_id=package_id,
                channel=channel,
                target_version_id=body_target_version_id,
                expected_channel_version=expected_channel_version,
                actor_id=actor_id,
                reason=body_reason,
                workspace_id=body_workspace_id,
                session=session,
            )
            await _emit_class_a_audit(
                session,
                action=audit_action,
                org_id=org_id,
                actor=actor,
                resource=_audit_resource(type_="release_channel", id_=ch_row.id, org_id=org_id),
                payload={
                    "channel_id": ch_row.id,
                    "package_id": package_id,
                    "channel": channel,
                    "from_version_id": ev_row.from_version_id,
                    "to_version_id": ev_row.to_version_id,
                    "row_version": ch_row.row_version,
                    "action": ev_row.action,
                },
            )
            promote_response = PromoteResponse(
                channel=_channel_response(ch_row),
                event=_event_response(ev_row),
            )
            if idem_key is not None:
                await insert_idempotency_record(
                    session,
                    org_id=org_id,
                    idempotency_key=idem_key,
                    request_hash=request_hash,
                    response_payload=promote_response.model_dump(mode="json"),
                    status_code=status.HTTP_200_OK,
                    record_id=_new_uuid(),
                )
            await session.commit()
    except _ReplayHit as hit:
        # Replay short-circuits before any mutation; the stored row_version is
        # the original winner's, so ETag is correct.
        return hit.response
    except IntegrityError:
        # The UNIQUE(org_id, idempotency_key) fence fired — a concurrent
        # same-key writer committed between our happy-path read and our insert.
        # Only reachable when idem_key is set. Re-classify on a fresh session.
        if idem_key is None:
            raise  # unexpected — re-raise so it surfaces as a 500
        outcome, record = await resolve_idempotency_outcome(
            sf,
            org_id=org_id,
            idempotency_key=idem_key,
            request_hash=request_hash,
        )
        if outcome == "replay" and record is not None:
            replayed = PromoteResponse.model_validate(record.response_payload)
            _set_etag(response, replayed.channel.row_version)
            return replayed
        if outcome == "conflict":
            raise IdempotencyConflictError(org_id=org_id, idempotency_key=idem_key)
        # outcome == "miss" — the winner rolled back; surface 409 so the
        # caller backs off and retries the whole request.
        raise _contract_error_response(
            request,
            ErrorCode.RELEASE_CONFLICT,
            status_code=status.HTTP_409_CONFLICT,
            message="idempotency race: concurrent writer rolled back; retry the request.",
        ) from None

    _set_etag(response, promote_response.channel.row_version)
    return promote_response


def _new_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


@router.get(
    "/agent-packages/{package_id}/channels",
    response_model=list[ReleaseChannelResponse],
)
@require_rbac(Permission.STUDIO_PACKAGE_READ)
async def list_package_channels(request: Request, package_id: str) -> list[ReleaseChannelResponse]:
    """List channels for a package in the caller's Org (ADR §5)."""
    org_id = _require_org_id(request)
    # Verify the package belongs to the caller's Org before listing channels.
    pkg = await get_agent_package(_sf(request), package_id=package_id, org_id=org_id)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found.")
    rows = await list_channels(_sf(request), org_id=org_id, package_id=package_id)
    return [_channel_response(r) for r in rows]


@router.get(
    "/agent-packages/{package_id}/channels/{channel}",
    response_model=ReleaseChannelResponse,
)
@require_rbac(Permission.STUDIO_PACKAGE_READ)
async def get_package_channel(request: Request, package_id: str, channel: str) -> ReleaseChannelResponse:
    """Get one channel. Cross-Org / unknown-channel miss → 404 (existence-hiding)."""
    org_id = _require_org_id(request)
    _validate_channel(channel)
    pkg = await get_agent_package(_sf(request), package_id=package_id, org_id=org_id)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found.")
    row = await get_channel(_sf(request), org_id=org_id, package_id=package_id, channel=channel, workspace_id=None)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found.")
    return _channel_response(row)


@router.get(
    "/agent-packages/{package_id}/channels/{channel}/events",
    response_model=list[ReleaseEventResponse],
)
@require_rbac(Permission.STUDIO_PACKAGE_READ)
async def list_package_channel_events(request: Request, package_id: str, channel: str) -> list[ReleaseEventResponse]:
    """List promote/rollback events for a channel (ADR §14 domain history)."""
    org_id = _require_org_id(request)
    _validate_channel(channel)
    pkg = await get_agent_package(_sf(request), package_id=package_id, org_id=org_id)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found.")
    ch = await get_channel(_sf(request), org_id=org_id, package_id=package_id, channel=channel, workspace_id=None)
    if ch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found.")
    rows = await list_events(_sf(request), org_id=org_id, channel_id=ch.id)
    return [_event_response(r) for r in rows]


@router.post(
    "/agent-packages/{package_id}/channels/{channel}:promote",
    response_model=PromoteResponse,
)
@require_rbac(Permission.STUDIO_RELEASE_PROMOTE_DEV)
async def promote_package_channel(
    request: Request,
    response: Response,
    package_id: str,
    channel: str,
    body: PromoteRequest,
) -> PromoteResponse:
    """Promote a Version onto a channel via CAS (ADR §7).

    The CAS predicate (``expected_channel_version``) is sourced with
    ``If-Match`` header precedence, falling back to the body field (PR-055
    dual-track; exactly one MUST be present). On a concurrent promote only one
    caller wins; the others get 409 ``release_conflict``. An ``Idempotency-Key``
    header makes the call a replay-safe idempotent op: same key + same request
    returns the original result (no second CAS move, no second audit row);
    same key + different request returns 409 ``idempotency_conflict``.

    Dynamic permission gate: dev accepts ``promote_dev`` OR ``promote``;
    staging/prod require ``promote``. Emits ``release.agent.published``
    (channel CAS success — DISTINCT from ``catalog.agent_version.published``).
    """
    org_id = _require_org_id(request)
    _validate_channel(channel)
    await _require_promote_permission(request, channel)
    sf = _sf(request)
    # Verify the package belongs to the caller's Org (defense-in-depth: the
    # repository also checks, but a 404 here is cheaper than a ValueError→404).
    pkg = await get_agent_package(sf, package_id=package_id, org_id=org_id)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found.")
    try:
        return await _orchestrate_idempotent_move(
            request,
            action=EVENT_ACTION_PROMOTE,
            org_id=org_id,
            package_id=package_id,
            channel=channel,
            body_target_version_id=body.target_version_id,
            body_expected_channel_version=body.expected_channel_version,
            body_workspace_id=body.workspace_id,
            body_reason=body.reason,
            actor_id=_actor_id(request),
            actor=_audit_actor(request),
            move_fn=promote_channel,
            audit_action="release.agent.published",
            response=response,
        )
    except ReleaseConflictError as exc:
        raise _contract_error_response(
            request,
            ErrorCode.RELEASE_CONFLICT,
            status_code=status.HTTP_409_CONFLICT,
            message=str(exc),
            details={"reason": "cas_miss"},
        ) from exc
    except ChannelGateError as exc:
        raise _contract_error_response(
            request,
            ErrorCode.RELEASE_GATE_VIOLATION,
            status_code=status.HTTP_409_CONFLICT,
            message=str(exc),
        ) from exc
    except IdempotencyConflictError as exc:
        raise _contract_error_response(
            request,
            ErrorCode.IDEMPOTENCY_CONFLICT,
            status_code=status.HTTP_409_CONFLICT,
            message=str(exc),
            details={"idempotency_key": exc.idempotency_key},
        ) from exc
    except ValueError as exc:
        # target Version absent / wrong Org / wrong package → existence-hiding 404.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.") from exc


@router.post(
    "/agent-packages/{package_id}/channels/{channel}:rollback",
    response_model=PromoteResponse,
)
@require_rbac(Permission.STUDIO_RELEASE_ROLLBACK)
async def rollback_package_channel(
    request: Request,
    response: Response,
    package_id: str,
    channel: str,
    body: RollbackRequest,
) -> PromoteResponse:
    """Rollback a channel to a historical Version via CAS (ADR §8).

    Rollback moves the pointer without modifying Version content. prod
    rollback requires the target be published and non-revoked. Emits
    ``release.agent.rolled_back``. Same ``If-Match`` / ``Idempotency-Key``
    semantics as promote (PR-055).
    """
    org_id = _require_org_id(request)
    _validate_channel(channel)
    sf = _sf(request)
    actor = _audit_actor(request)
    pkg = await get_agent_package(sf, package_id=package_id, org_id=org_id)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found.")
    try:
        return await _orchestrate_idempotent_move(
            request,
            action=EVENT_ACTION_ROLLBACK,
            org_id=org_id,
            package_id=package_id,
            channel=channel,
            body_target_version_id=body.target_version_id,
            body_expected_channel_version=body.expected_channel_version,
            body_workspace_id=body.workspace_id,
            body_reason=body.reason,
            actor_id=_actor_id(request),
            actor=actor,
            move_fn=rollback_channel,
            audit_action="release.agent.rolled_back",
            response=response,
        )
    except ReleaseConflictError as exc:
        raise _contract_error_response(
            request,
            ErrorCode.RELEASE_CONFLICT,
            status_code=status.HTTP_409_CONFLICT,
            message=str(exc),
            details={"reason": "cas_miss"},
        ) from exc
    except ChannelGateError as exc:
        raise _contract_error_response(
            request,
            ErrorCode.RELEASE_GATE_VIOLATION,
            status_code=status.HTTP_409_CONFLICT,
            message=str(exc),
        ) from exc
    except IdempotencyConflictError as exc:
        raise _contract_error_response(
            request,
            ErrorCode.IDEMPOTENCY_CONFLICT,
            status_code=status.HTTP_409_CONFLICT,
            message=str(exc),
            details={"idempotency_key": exc.idempotency_key},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.") from exc
