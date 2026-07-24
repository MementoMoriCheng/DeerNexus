"""Tenant audit-event interface — outbox-backed (PR-041).

This module was the **explicit, non-silent** event sink mandated by
``pr-split-guide.md`` §7 (PR-022) while the Audit outbox was unbuilt: a single
``logger.info`` choke-point so no tenant-lifecycle event was silently dropped.
PR-041 upgrades it: when the app layer registers an ``AuditSink``
(``set_tenant_event_sink``), events are enqueued into the transactional
outbox (``audit_outbox``) for durable, retried, idempotent delivery to the
append-only ``audit_events`` store (PR-040) by the background worker.

Harness boundary (load-bearing): this module is in the harness package
(``deerflow.tenancy``) and MUST NOT import the app layer (``app.gateway``),
which ``test_harness_boundary`` enforces. So the app registers a concrete
``AuditSink`` Protocol object here at lifespan startup, and the shim calls
``sink.emit(event)`` without knowing how the sink persists. Before
registration (tests, the first moments of boot), the sink is ``None`` and the
shim falls back to ``logger.info`` so events are still observable, not lost.

Contract (unchanged from PR-022): this function MUST NOT raise on a sink /
logging failure (events are best-effort observability, never a correctness
gate on this best-effort path), and MUST NOT silently no-op — at minimum the
event is logged at INFO level. The Class A fail-closed guarantee (business
write rolls back if the outbox write fails, ADR §7.1) is delivered by the
PR-042 same-transaction path: Class A router endpoints call
:func:`build_audit_event` + :func:`enqueue_audit_outbox_in_session` inside a
single ``async with sf() as session:`` block and commit atomically. This shim
remains the best-effort default for paths that are NOT in the Class A set
(bootstrap, backfill, OIDC mapping engine, last-admin guard) so those events
are still durable (post-commit) but not transactionally coupled to the write.

The ``AuditEvent`` built by the best-effort shim is a projection of the
shim's 3-arg shape (``event_type``, ``org_id``, ``principal_id``,
``payload``): the ``action`` is normalized via :data:`TENANT_EVENT_ACTION_REGISTRY`
(``<domain>.<resource>.<verb>``, ADR §4), the actor is a minimal
``PrincipalRef`` (``user`` when a principal_id is present, else ``system``),
``request_id`` comes from the active ``CorrelationContext`` if any, and
``outcome`` is ``success`` (all shim callers are success-path). The Class A
router path uses :func:`build_audit_event` instead, which takes the real
actor / resource / outcome at the call site.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from deerflow.contracts.events import AuditEvent, AuditOutcome
from deerflow.contracts.identity import PrincipalRef
from deerflow.contracts.policy import ResourceRef

logger = logging.getLogger(__name__)

#: Legacy shim ``event_type`` → normalized ADR §4 ``<domain>.<resource>.<verb>``
#: action. Every ``emit_tenant_event`` call site uses a legacy event_type; this
#: registry projects it onto the compliance action namespace so audit events are
#: queryable by domain (``iam.*``) regardless of which path emitted them. A
#: missing key falls back to the raw event_type (defensive — never silently
#: renames an unmapped event). Maintained here as the single source of truth so
#: both the best-effort shim path (PR-041) and the Class A same-transaction path
#: (PR-042) emit identical action strings.
TENANT_EVENT_ACTION_REGISTRY: Mapping[str, str] = {
    # ServiceAccount lifecycle (iam.py)
    "service_account_created": "iam.service_account.created",
    "service_account_updated": "iam.service_account.updated",
    "service_account_disabled": "iam.service_account.disabled",
    "service_account_active": "iam.service_account.activated",
    "service_account_deleted": "iam.service_account.deleted",
    # Role bindings (iam.py — service-account scope today; user bindings via bootstrap)
    "service_account_role_binding_created": "iam.role_binding.created",
    "service_account_role_binding_deleted": "iam.role_binding.deleted",
    "admin_role_binding_created": "iam.role_binding.created",
    # API Keys (iam.py)
    "api_key_created": "iam.api_key.created",
    "api_key_revoked": "iam.api_key.revoked",
    # Org memberships (iam.py)
    "org_membership_suspended": "iam.membership.suspended",
    "org_membership_activated": "iam.membership.activated",
    "admin_membership_created": "iam.membership.created",
    # OIDC group mappings (iam.py + oidc_group_mapping.py)
    "oidc_group_mapping_created": "iam.oidc_group_mapping.created",
    "oidc_group_mapping_updated": "iam.oidc_group_mapping.updated",
    "oidc_group_mapping_deleted": "iam.oidc_group_mapping.deleted",
    "oidc_group_mapping_applied": "iam.oidc_group_mapping.applied",
    # Org / bootstrap (system-initiated; best-effort shim path)
    "default_org_created": "org.default.created",
    "default_org_exists": "org.default.exists",
    "validation_org_created": "org.validation.created",
    "validation_org_exists": "org.validation.exists",
    "builtin_role_created": "iam.role.created",
    # Backfill (system-initiated)
    "backfill_started": "org.backfill.started",
    "backfill_completed": "org.backfill.completed",
    # Class B runtime-security events (PR-044, ADR §7.2 / §5.4). The guardrail
    # tool-call deny path (harness layer) emits via ``emit_tenant_event`` and
    # relies on this normalization; the app-layer login + RBAC-deny paths emit
    # directly via ``build_audit_event`` with the already-normalized action.
    "policy_tool_denied": "policy.tool.denied",
    "auth_login": "auth.login",
}


def _resolve_action(event_type: str) -> str:
    """Map a legacy shim ``event_type`` to the normalized ADR §4 action.

    Unknown keys pass through unchanged (defensive — an unmapped event is still
    emitted under its raw name and surfaces in review rather than silently
    renamed).
    """
    return TENANT_EVENT_ACTION_REGISTRY.get(event_type, event_type)


#: The app-layer sink injected at lifespan startup (harness cannot import app).
#: ``None`` until ``set_tenant_event_sink`` is called → falls back to logger.
_registered_sink: Any = None


def set_tenant_event_sink(sink: Any | None) -> None:
    """Register (or clear) the app-layer ``AuditSink`` for tenant events.

    Called by the gateway lifespan once the engine is up
    (``app.gateway.app`` passes ``get_audit_sink()``). Passing ``None``
    restores the logger-only fallback (used by tests that do not boot the
    outbox). Idempotent: re-registering replaces the previous sink.
    """
    global _registered_sink
    _registered_sink = sink


def _registered_sink_get() -> Any:
    """Return the currently registered sink (``None`` if unset). Test hook."""
    return _registered_sink


def _build_event(
    event_type: str,
    *,
    org_id: str | None,
    principal_id: str | None,
    payload: Mapping[str, Any] | None,
    outcome: AuditOutcome = "success",
) -> AuditEvent:
    """Project the shim's 3-arg shape onto a best-effort ``AuditEvent``.

    See module docstring for the projection rules. ``request_id`` defaults to
    ``"system"`` (AuditEvent requires a non-empty correlation id) when no
    ``CorrelationContext`` is bound (background tasks, bootstrap). ``outcome``
    defaults to ``success`` (every legacy Class A caller is a success path);
    Class B callers (PR-044) pass ``denied`` / ``failure`` explicitly.
    """
    # Best-effort request_id from the active correlation context; absent in
    # background tasks → "system".
    request_id = "system"
    try:
        from deerflow.observability.correlation import get_correlation

        ctx = get_correlation()
        if ctx is not None and ctx.request_id:
            request_id = ctx.request_id
    except Exception:  # noqa: BLE001
        pass

    if principal_id:
        actor = PrincipalRef(type="user", id=principal_id, user_id=principal_id)
    else:
        # System events (bootstrap, builtin_role_created, backfill) have no
        # human actor. id must be non-empty; "system" is the stable sentinel.
        actor = PrincipalRef(type="system", id="system")

    return AuditEvent(
        event_id=uuid.uuid4().hex,
        idempotency_key=f"tenant-event:{event_type}:{uuid.uuid4().hex}",
        org_id=org_id,
        actor=actor,
        action=_resolve_action(event_type),
        outcome=outcome,
        request_id=request_id,
        occurred_at=datetime.now(UTC),
        payload=dict(payload) if payload else {},
    )


def build_audit_event(
    action: str,
    *,
    org_id: str | None,
    actor: PrincipalRef,
    outcome: AuditOutcome = "success",
    resource: ResourceRef | None = None,
    reason_code: str | None = None,
    payload: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
    run_id: str | None = None,
) -> AuditEvent:
    """Construct a fully-specified ``AuditEvent`` for the Class A path (PR-042).

    Unlike the best-effort :func:`_build_event` shim projection (which invents a
    minimal actor and hard-codes ``outcome="success"``), the Class A router path
    has the real authenticated actor, the affected resource, and the business
    outcome at hand. This helper assembles them into an ``AuditEvent`` ready for
    same-transaction enqueue via
    :func:`deerflow.persistence.audit.outbox.enqueue_audit_outbox_in_session`.

    ``action`` is passed ALREADY normalized to the ``<domain>.<resource>.<verb>``
    form (ADR §4) by the caller — the caller knows the domain and verb at the
    call site, so we do not second-guess it here. ``request_id`` is sourced from
    the active ``CorrelationContext`` (falling back to ``"system"`` for
    background tasks) exactly as the shim does. A fresh ``event_id`` is generated
    per call (§9.1 — event_id is produced inside the business transaction).
    """
    request_id = "system"
    resolved_trace_id = trace_id
    try:
        from deerflow.observability.correlation import get_correlation

        ctx = get_correlation()
        if ctx is not None:
            if ctx.request_id:
                request_id = ctx.request_id
            if resolved_trace_id is None and getattr(ctx, "trace_id", None):
                resolved_trace_id = ctx.trace_id
    except Exception:  # noqa: BLE001
        pass

    return AuditEvent(
        event_id=uuid.uuid4().hex,
        idempotency_key=f"{action}:{uuid.uuid4().hex}",
        org_id=org_id,
        actor=actor,
        action=action,
        outcome=outcome,
        reason_code=reason_code,
        request_id=request_id,
        trace_id=resolved_trace_id,
        run_id=run_id,
        occurred_at=datetime.now(UTC),
        resource=resource,
        payload=dict(payload) if payload else {},
    )


def emit_tenant_event(
    event_type: str,
    *,
    org_id: str | None,
    principal_id: str | None,
    payload: Mapping[str, Any] | None = None,
    outcome: AuditOutcome = "success",
) -> None:
    """Record a tenant-lifecycle event (outbox-backed when a sink is registered).

    When the app has registered an ``AuditSink`` (``set_tenant_event_sink``),
    the event is projected onto an ``AuditEvent`` and enqueued for durable
    delivery. The sink's ``emit`` is awaited if it is a coroutine, or called
    directly if synchronous; either way a failure is swallowed (best-effort,
    never raises — same contract as the PR-022 logger fallback) and the event
    is still logged at INFO so nothing is silently lost.

    With no registered sink (pre-boot, tests), this is the original structured
    ``logger.info`` so events remain observable.

    ``outcome`` defaults to ``success`` (every legacy Class A caller is a
    success path); Class B callers (PR-044) pass ``denied`` / ``failure``.
    """
    # Always log first: even with a sink, the log line is the observable
    # floor and survives a sink that itself fails. INFO, structured.
    logger.info(
        "tenant-event type=%s org=%s principal=%s outcome=%s payload=%s",
        event_type,
        org_id,
        principal_id,
        outcome,
        dict(payload) if payload else {},
    )

    sink = _registered_sink
    if sink is None:
        return

    try:
        event = _build_event(event_type, org_id=org_id, principal_id=principal_id, payload=payload, outcome=outcome)
        result = sink.emit(event)
        # Support both async sinks (OutboxAuditSink) and sync Protocol impls.
        import asyncio

        if asyncio.iscoroutine(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                # We are inside an event loop — schedule the enqueue so the
                # caller (a sync-ish harness path) is not blocked. The sink's
                # own try/except guarantees it never raises.
                loop.create_task(result)
            else:
                # No running loop (rare sync bootstrap path): run to completion.
                asyncio.run(result)
    except Exception:  # noqa: BLE001
        # Never raise: best-effort observability, never a correctness gate.
        logger.warning("tenant-event sink emit failed for type=%s", event_type, exc_info=True)
