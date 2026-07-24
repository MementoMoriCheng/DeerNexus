"""Admin / Org Console API (PR-060).

Three read-only endpoints scoped to the caller's active Org:

- ``GET /api/v1/admin/stats`` — run status rollup + recent activity counters
- ``GET /api/v1/admin/runs`` — keyset-paginated run listing with status /
  model / time-window filters
- ``GET /api/v1/admin/usage`` — org-level token usage aggregation

These power the three Console entry points defined for PR-061 Admin Console
UI (Runs, Usage, Failure/Audit — the last reuses ``/runs?status=error,...``
and the stats rollup rather than introducing a separate audit endpoint).

Gating: ``@require_rbac(Permission.ADMIN_CONSOLE_READ)`` — the
DB-backed Authorize Service (PR-031) gates every endpoint against the
caller's effective permission set. ``org:admin`` carries
``admin:console:read`` (ADR-0003 §4.1); ``org:developer`` / ``org:viewer``
do not, so they receive 403. ``system_role == "admin"`` users must still
carry an ``org:admin`` RoleBinding (seeded by ``/initialize`` /
``seed-admin-iam``) — the ``system_role`` field alone is not a grant.

Data source: existing ``RunRow`` token columns + ``token_usage_by_model``
JSON. ``UsageRecord`` persistence is deferred (the contract requires
``release_digest``, coupled to the undelivered Track E PR-054 Release
Resolver); ``cost_*`` fields are not returned — there is no price table
today, and §7.1 forbids shipping fabricated values.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.gateway.deps import get_run_store
from app.gateway.pagination import decode_cursor, encode_cursor
from app.gateway.rbac import require_rbac
from app.gateway.routers.thread_runs import (
    ThreadTokenUsageCallerBreakdown,
    ThreadTokenUsageModelBreakdown,
)
from deerflow.contracts import Permission, get_tenant_context
from deerflow.persistence.audit import list_audit_events
from deerflow.utils.time import coerce_iso

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# RunRow.error is free text and may carry a secret (DSN fragment, prompt
# excerpt). Truncate to keep the response bounded and scrub forbidden
# substrings per the §3.3 forbidden-field list.
_MAX_ERROR_PREVIEW_CHARS = 200

# Lower-cased forbidden substrings that should never surface in a run error
# preview, even truncated. Mirrors the spirit of
# ``deerflow.observability.scrubbing.FORBIDDEN_EXTRA_KEYS`` but operates on
# free text rather than dict keys, so a contiguous-substring check is the
# right granularity: ``Authorization: Bearer abc`` must never leak.
_FORBIDDEN_ERROR_SUBSTRINGS: tuple[str, ...] = (
    "authorization",
    "bearer",
    "secret",
    "password",
    "token=",
    "api_key",
    "apikey",
    "dsn",
    "claims",
)


def _scrub_error_preview(error: str | None) -> str | None:
    """Return a bounded, scrubbed preview of a run's error text.

    Returns ``None`` when the original is ``None`` (no error). When the
    truncated preview contains a §3.3 forbidden substring, the whole
    preview is replaced with ``"<redacted>"`` — truncation can still leak
    the first bytes of a secret, so once the scrubber fires we do not echo
    any fragment.
    """
    if not error:
        return None
    preview = error[:_MAX_ERROR_PREVIEW_CHARS]
    lowered = preview.lower()
    if any(word in lowered for word in _FORBIDDEN_ERROR_SUBSTRINGS):
        return "<redacted>"
    return preview


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class OrgStatsResponse(BaseModel):
    org_id: str
    window_start: str
    window_end: str
    total_runs: int = 0
    runs_by_status: dict[str, int] = Field(default_factory=dict)
    failure_rate: float = 0.0
    recent_runs_24h: int = 0
    recent_failures_24h: int = 0


class OrgRunSummary(BaseModel):
    run_id: str
    thread_id: str
    user_id: str | None = None
    status: str
    model_name: str | None = None
    created_at: str = ""
    updated_at: str = ""
    total_tokens: int = 0
    error: str | None = None


class OrgRunListResponse(BaseModel):
    data: list[OrgRunSummary] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None


class OrgTokenUsageResponse(BaseModel):
    org_id: str
    window_start: str
    window_end: str
    total_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_runs: int = 0
    by_model: dict[str, ThreadTokenUsageModelBreakdown] = Field(default_factory=dict)
    by_caller: ThreadTokenUsageCallerBreakdown = Field(default_factory=ThreadTokenUsageCallerBreakdown)


class AuditActorRef(BaseModel):
    """Audit event actor projection (flattened ORM columns re-nested)."""

    type: str
    id: str
    user_id: str | None = None
    display_name: str | None = None


class AuditResourceRef(BaseModel):
    """Audit event resource projection (None when the event had no resource)."""

    type: str
    id: str | None = None
    org_id: str | None = None


class AuditEventResponse(BaseModel):
    """One audit event as returned by ``GET /audit/events`` (ADR §12.1).

    The ``payload`` is the already-scrubbed form written by PR-040's
    ``_scrub_payload`` at insert time — the query path performs no read-side
    re-scrubbing (§12.1 "查询响应不返回被脱敏字段" is satisfied at write time).
    """

    event_id: str
    occurred_at: datetime
    action: str
    outcome: str
    reason_code: str | None = None
    actor: AuditActorRef
    resource: AuditResourceRef | None = None
    request_id: str
    run_id: str | None = None
    org_id: str | None = None
    payload: dict = Field(default_factory=dict)


class AuditEventListResponse(BaseModel):
    data: list[AuditEventResponse] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None


#: ADR-0005 §12.1 "默认 24 小时" — when neither ``occurred_after`` nor
#: ``occurred_before`` is supplied, the window defaults to the trailing 24h.
_DEFAULT_AUDIT_QUERY_WINDOW = timedelta(hours=24)

#: ADR-0005 §12.1 "在线查询最大 90 天" — an online query window wider than this
#: is rejected with 400; unbounded windows are the async export job's
#: responsibility (§12.3, deferred).
_MAX_AUDIT_QUERY_WINDOW = timedelta(days=90)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_org_id(request: Request) -> str:
    """Resolve the caller's active org_id from the bound TenantContext.

    The Org Console is per-Org: an admin in Org A cannot see Org B's runs
    via this API. ``AUTO_ORG`` is the sentinel that resolves against the
    contextvar bound by ``TenantResolutionMiddleware``; when no tenant is
    bound (auth-disabled mode, CLI path) we fail closed with 400 — the
    Org Console has no meaning outside a tenant context.
    """
    ctx = get_tenant_context()
    if ctx is None or not ctx.org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Org Console requires an active tenant context; none is bound.",
        )
    return ctx.org_id


def _to_org_run_summary(row: dict[str, Any]) -> OrgRunSummary:
    return OrgRunSummary(
        run_id=str(row.get("run_id", "")),
        thread_id=str(row.get("thread_id", "")),
        user_id=row.get("user_id"),
        status=str(row.get("status", "")),
        model_name=row.get("model_name"),
        created_at=coerce_iso(row.get("created_at")) if row.get("created_at") else "",
        updated_at=coerce_iso(row.get("updated_at")) if row.get("updated_at") else "",
        total_tokens=int(row.get("total_tokens", 0) or 0),
        error=_scrub_error_preview(row.get("error")),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=OrgStatsResponse)
@require_rbac(Permission.ADMIN_CONSOLE_READ)
async def org_stats(
    request: Request,
    since: datetime | None = Query(default=None, description="Window start (UTC). Defaults to now-7d."),
    until: datetime | None = Query(default=None, description="Window end (UTC). Defaults to now."),
) -> OrgStatsResponse:
    """Run status rollup for the caller's active Org."""
    org_id = _require_org_id(request)
    run_store = get_run_store(request)
    if run_store is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Run store is not configured.")
    agg = await run_store.aggregate_stats_by_org(org_id, since=since, until=until)
    return OrgStatsResponse(
        org_id=org_id,
        window_start=coerce_iso(agg["window_start"]),
        window_end=coerce_iso(agg["window_end"]),
        total_runs=int(agg["total_runs"]),
        runs_by_status={str(k): int(v) for k, v in agg["runs_by_status"].items()},
        failure_rate=float(agg["failure_rate"]),
        recent_runs_24h=int(agg["recent_runs_24h"]),
        recent_failures_24h=int(agg["recent_failures_24h"]),
    )


@router.get("/runs", response_model=OrgRunListResponse)
@require_rbac(Permission.ADMIN_CONSOLE_READ)
async def org_runs(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status", description="Filter by run status."),
    model: str | None = Query(default=None, description="Filter by model_name."),
    since: datetime | None = Query(default=None, description="Run created_at >= this UTC timestamp."),
    until: datetime | None = Query(default=None, description="Run created_at <= this UTC timestamp."),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, description="Opaque cursor from a prior page's next_cursor."),
) -> OrgRunListResponse:
    """Keyset-paginated run listing for the caller's active Org."""
    org_id = _require_org_id(request)
    run_store = get_run_store(request)
    if run_store is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Run store is not configured.")

    decoded_cursor: tuple[datetime, str] | None = None
    if cursor is not None:
        try:
            decoded_cursor = decode_cursor(cursor)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed cursor token.",
            ) from None

    rows, has_more = await run_store.list_runs_by_org(
        org_id,
        status=status_filter,
        model=model,
        since=since,
        until=until,
        limit=limit,
        cursor=decoded_cursor,
    )

    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        last_created = last.get("created_at")
        last_run_id = str(last.get("run_id", ""))
        if isinstance(last_created, datetime):
            next_cursor = encode_cursor(last_created, last_run_id)
        elif last_created:
            try:
                next_cursor = encode_cursor(datetime.fromisoformat(str(last_created)), last_run_id)
            except ValueError:
                next_cursor = None

    return OrgRunListResponse(
        data=[_to_org_run_summary(r) for r in rows],
        has_more=has_more,
        next_cursor=next_cursor,
    )


@router.get("/usage", response_model=OrgTokenUsageResponse)
@require_rbac(Permission.ADMIN_CONSOLE_READ)
async def org_usage(
    request: Request,
    since: datetime | None = Query(default=None, description="Window start (UTC). Defaults to unbounded."),
    until: datetime | None = Query(default=None, description="Window end (UTC). Defaults to unbounded."),
    include_active: bool = Query(default=False, description="Include running runs in the aggregation."),
) -> OrgTokenUsageResponse:
    """Org-level token usage aggregation for the caller's active Org."""
    org_id = _require_org_id(request)
    run_store = get_run_store(request)
    if run_store is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Run store is not configured.")
    agg = await run_store.aggregate_tokens_by_org(
        org_id,
        since=since,
        until=until,
        include_active=include_active,
    )
    return OrgTokenUsageResponse(
        org_id=org_id,
        window_start=coerce_iso(since) if since else "",
        window_end=coerce_iso(until) if until else "",
        total_tokens=int(agg["total_tokens"]),
        total_input_tokens=int(agg["total_input_tokens"]),
        total_output_tokens=int(agg["total_output_tokens"]),
        total_runs=int(agg["total_runs"]),
        by_model={
            str(model): ThreadTokenUsageModelBreakdown(
                tokens=int(entry.get("tokens", 0)),
                runs=int(entry.get("runs", 0)),
            )
            for model, entry in agg["by_model"].items()
        },
        by_caller=ThreadTokenUsageCallerBreakdown(
            lead_agent=int(agg["by_caller"]["lead_agent"]),
            subagent=int(agg["by_caller"]["subagent"]),
            middleware=int(agg["by_caller"]["middleware"]),
        ),
    )


# ---------------------------------------------------------------------------
# Audit event query (PR-045, ADR-0005 §12.1)
# ---------------------------------------------------------------------------


def _to_audit_event_response(row) -> AuditEventResponse:
    """Project a flat ``AuditEventRow`` onto the nested API response model.

    The ORM stores ``actor`` / ``resource`` as flattened columns (PR-040,
    indexable + round-trip lossless); this rebuilds the nested shape the API
    contract exposes. ``payload`` is already scrubbed at write time.
    """
    resource: AuditResourceRef | None = None
    if row.resource_type is not None:
        resource = AuditResourceRef(
            type=row.resource_type,
            id=row.resource_id,
            org_id=row.resource_org_id,
        )
    return AuditEventResponse(
        event_id=row.event_id,
        occurred_at=row.occurred_at,
        action=row.action,
        outcome=row.outcome,
        reason_code=row.reason_code,
        actor=AuditActorRef(
            type=row.actor_type,
            id=row.actor_id,
            user_id=row.actor_user_id,
            display_name=row.actor_display_name,
        ),
        resource=resource,
        request_id=row.request_id,
        run_id=row.run_id,
        org_id=row.org_id,
        payload=dict(row.payload) if row.payload else {},
    )


@router.get("/audit/events", response_model=AuditEventListResponse)
@require_rbac(Permission.ADMIN_AUDIT_READ)
async def org_audit_events(
    request: Request,
    action: str | None = Query(default=None, description="Filter by ADR §4 action (e.g. 'iam.role_binding.created')."),
    actor_id: str | None = Query(default=None, description="Filter by actor id."),
    resource_type: str | None = Query(default=None, description="Filter by resource type."),
    resource_id: str | None = Query(default=None, description="Filter by resource id."),
    outcome: str | None = Query(default=None, description="Filter by outcome (success/denied/failure)."),
    run_id: str | None = Query(default=None, description="Filter by associated run id."),
    request_id: str | None = Query(default=None, description="Filter by correlation request id."),
    occurred_after: datetime | None = Query(default=None, description="Window start (UTC). Defaults to the trailing 24h."),
    occurred_before: datetime | None = Query(default=None, description="Window end (UTC). Defaults to now."),
    limit: int = Query(default=100, ge=1, le=100, description="Page size (max 100)."),
    cursor: str | None = Query(default=None, description="Opaque cursor from a prior page's next_cursor."),
) -> AuditEventListResponse:
    """Org-scoped audit-event query with cursor pagination (ADR-0005 §12.1).

    ``org_id`` is forced from the bound TenantContext and the repository
    hard-filters on it (§12.1 "强制 Org"); a cross-Org system-admin query is a
    separate, separately-audited path (§12.2, not this endpoint). The seven
    filters mirror the §12.1 allow-list. The time window defaults to the
    trailing 24h and is capped at 90 days for the online path (§12.1).
    """
    org_id = _require_org_id(request)

    now = datetime.now(UTC)
    window_start = occurred_after
    window_end = occurred_before if occurred_before is not None else now
    if window_start is None:
        window_start = window_end - _DEFAULT_AUDIT_QUERY_WINDOW
    # §12.1 online-query 90-day cap. A wider window needs the async export job.
    if window_end - window_start > _MAX_AUDIT_QUERY_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Online audit query window exceeds the 90-day maximum; use the async export job for wider ranges.",
        )

    decoded_cursor: tuple[datetime, str] | None = None
    if cursor is not None:
        try:
            decoded_cursor = decode_cursor(cursor)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed cursor token.",
            ) from None

    from deerflow.persistence.engine import get_session_factory

    sf = get_session_factory()
    if sf is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audit query requires persistence; no session factory is configured.",
        )
    # Probe one extra row to derive has_more without changing the repository
    # signature (list_audit_events returns a plain list; the runs path does
    # the same limit+1 trick inside its store).
    rows = await list_audit_events(
        sf,
        org_id=org_id,
        action=action,
        actor_id=actor_id,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        run_id=run_id,
        request_id=request_id,
        occurred_after=window_start,
        occurred_before=window_end,
        cursor=decoded_cursor,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(last.occurred_at, last.event_id)

    return AuditEventListResponse(
        data=[_to_audit_event_response(r) for r in rows],
        has_more=has_more,
        next_cursor=next_cursor,
    )
