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
write rolls back if the outbox write fails, ADR §7.1) is the PR-042
same-transaction wiring; this shim is the best-effort default that lights up
every existing call site (PR-034/035/036/037's 28+ ``emit_tenant_event``
callers) without touching them.

The ``AuditEvent`` built here is a best-effort projection of the shim's
3-arg shape (``event_type``, ``org_id``, ``principal_id``, ``payload``): the
``action`` is the raw ``event_type``, the actor is a minimal ``PrincipalRef``
(``user`` when a principal_id is present, else ``system``), ``request_id``
comes from the active ``CorrelationContext`` if any, ``outcome`` is
``success`` (all current callers are success-path), and a fresh ``event_id``
is generated per call. Action normalization to ``<domain>.<resource>.<verb>``
and denied/failure outcomes land in PR-042's per-call-site rewrite.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from deerflow.contracts.events import AuditEvent
from deerflow.contracts.identity import PrincipalRef

logger = logging.getLogger(__name__)

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
) -> AuditEvent:
    """Project the shim's 3-arg shape onto a best-effort ``AuditEvent``.

    See module docstring for the projection rules. ``request_id`` defaults to
    ``"system"`` (AuditEvent requires a non-empty correlation id) when no
    ``CorrelationContext`` is bound (background tasks, bootstrap).
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
        action=event_type,
        outcome="success",
        request_id=request_id,
        occurred_at=datetime.now(UTC),
        payload=dict(payload) if payload else {},
    )


def emit_tenant_event(
    event_type: str,
    *,
    org_id: str | None,
    principal_id: str | None,
    payload: Mapping[str, Any] | None = None,
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
    """
    # Always log first: even with a sink, the log line is the observable
    # floor and survives a sink that itself fails. INFO, structured.
    logger.info(
        "tenant-event type=%s org=%s principal=%s payload=%s",
        event_type,
        org_id,
        principal_id,
        dict(payload) if payload else {},
    )

    sink = _registered_sink
    if sink is None:
        return

    try:
        event = _build_event(event_type, org_id=org_id, principal_id=principal_id, payload=payload)
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
