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
from datetime import datetime
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
