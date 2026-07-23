"""App-layer AuditSink backed by the transactional outbox (PR-041).

``OutboxAuditSink`` is the first concrete implementation of the
``deerflow.contracts.events.AuditSink`` Protocol: ``emit(event)`` enqueues the
event into ``audit_outbox`` (``persistence.audit.outbox.enqueue_audit_outbox``),
where the background worker (``app.gateway.audit_worker``) later claims and
publishes it to the append-only ``audit_events`` store.

This is a strict upgrade over the pre-PR-041 ``logger.info`` sink: an emitted
event now survives a process restart (durable row), is retried with backoff on
publish failure, and is deduplicated by ``event_id``. The Class A *same-
transaction* guarantee (ADR §7.1 — the enqueue rolls back with the business
write) is wired by PR-042, which passes the caller's session into a
transactional enqueue variant; this sink's own-session enqueue is the
post-commit best-effort path the upgraded ``emit_tenant_event`` shim uses.

Singleton (mirrors ``get_authorize_service``): a module-global
``_default_sink`` is lazily constructed from ``get_session_factory()`` on first
use and reset for tests. Routers / the shim call ``get_audit_sink()`` directly
(no FastAPI ``Depends``), matching the AuthorizeService access pattern.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from deerflow.contracts.events import AuditEvent

logger = logging.getLogger(__name__)

_default_sink: OutboxAuditSink | None = None


class OutboxAuditSink:
    """``AuditSink`` that persists events into ``audit_outbox`` for async delivery.

    ``emit`` never raises on a persistence failure (best-effort enqueue, same
    contract as the logger shim it replaces): a transient DB failure is logged
    and the event is lost rather than crashing the caller's control-plane write.
    Durable same-transaction delivery (fail-closed on enqueue failure) is the
    PR-042 Class A wiring; this sink is the best-effort default that lights up
    every existing ``emit_tenant_event`` call site without touching them.
    """

    def __init__(self, sf: async_sessionmaker) -> None:
        self._sf = sf

    async def emit(self, event: AuditEvent) -> None:
        """Enqueue ``event`` into the outbox (best-effort, never raises).

        A duplicate ``event_id`` (replay/retry) raises ``IntegrityError`` at the
        DB — that is the idempotent path (§9.1), not an error to surface, so it
        is swallowed at INFO level. Any other failure is logged at WARNING and
        the event is dropped (the in-app append-only guarantee and the TTL-style
        retry budget make a single dropped enqueue non-fatal for best-effort
        events; Class A events use the PR-042 transactional path).
        """
        from sqlalchemy.exc import IntegrityError

        from deerflow.persistence.audit.outbox import enqueue_audit_outbox

        try:
            await enqueue_audit_outbox(self._sf, event)
        except IntegrityError:
            # Idempotent by event_id: a replay re-enqueued an already-queued
            # event. Not an error — the worker will publish it exactly once.
            logger.debug("audit outbox already has event_id=%s (idempotent enqueue)", event.event_id)
        except Exception:  # noqa: BLE001
            logger.warning("audit outbox enqueue failed for event_id=%s; event dropped", event.event_id, exc_info=True)


def get_audit_sink() -> OutboxAuditSink:
    """Return the process-wide audit sink, constructing it on first use.

    Mirrors ``get_authorize_service``: lazy from ``get_session_factory()``,
    raises ``RuntimeError`` if persistence is not initialised. The gateway
    lifespan registers this once the engine is up (``app.gateway.app``); the
    shim (``tenancy/audit_events``) calls it via the registered-sink indirection
    so it never hard-imports the app layer (harness boundary).
    """
    global _default_sink
    if _default_sink is not None:
        return _default_sink

    from deerflow.persistence.engine import get_session_factory

    sf = get_session_factory()
    if sf is None:
        raise RuntimeError("OutboxAuditSink requires persistence but no session factory is available (backend=memory / not initialised).")
    _default_sink = OutboxAuditSink(sf)
    return _default_sink


def reset_audit_sink_for_testing() -> None:
    """Drop the cached default sink. Tests call this after swapping the factory."""
    global _default_sink
    _default_sink = None


__all__ = [
    "OutboxAuditSink",
    "get_audit_sink",
    "reset_audit_sink_for_testing",
]
