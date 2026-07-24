"""Class B runtime-security audit emission helpers (PR-044).

ADR-0005 §7.2 Class B events (login, policy deny, require_approval, tenant
mismatch, sandbox violation, manual reconcile) are **best-effort durable**:
they are emitted via the post-action outbox path (``OutboxAuditSink.emit`` →
``enqueue_audit_outbox``) rather than the Class A same-transaction wiring
(ADR §7.1). Class B paths have no business write to couple the enqueue to
(login is pre-tenant, a deny is a decision not a mutation), so the enqueue
fires before the handler returns / raises but in its own transaction.

Reliability contract (§7.2 "在返回或结束相关动作前进入可靠本地 outbox"):
the enqueue happens BEFORE the action completes (``emit`` is awaited before
``return``/``raise``), so a durable pending row exists by the time the client
sees the outcome. The emit NEVER raises — a sink failure (DB down, unique
collision on event_id replay) is swallowed at WARNING so a Class B path
cannot be turned into a correctness failure by the audit layer. The §7.2
"所有持久化路径均不可写时 fail-closed" hardening (queue-full / all-paths-down
→ block the action) is a separate PR that needs a queue-watermark /
backpressure state machine and is intentionally out of scope here — this
helper matches the PR-041 ``OutboxAuditSink`` best-effort contract exactly.

Harness boundary: this module is in the app layer and MUST NOT be imported
by harness code (``test_harness_boundary`` enforces). The harness-side
equivalent (e.g. the guardrail tool-call deny path) uses
``deerflow.tenancy.audit_events.emit_tenant_event``, which routes through the
registered sink without importing app.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from deerflow.contracts.events import AuditOutcome
from deerflow.contracts.identity import PrincipalRef
from deerflow.contracts.policy import ResourceRef
from deerflow.tenancy.audit_events import build_audit_event

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


async def emit_class_b_audit(
    action: str,
    *,
    org_id: str | None,
    actor: PrincipalRef,
    outcome: AuditOutcome,
    reason_code: str | None = None,
    resource: ResourceRef | None = None,
    payload: dict | None = None,
) -> None:
    """Best-effort enqueue a Class B runtime-security audit event (ADR §7.2).

    Builds a fully-specified ``AuditEvent`` (real actor / outcome / resource /
    reason_code) and enqueues it via the registered ``OutboxAuditSink``. Never
    raises: a sink failure is logged at WARNING and the event is dropped
    (best-effort, matching the PR-041 sink contract). Call this in the Class B
    path BEFORE the handler returns/raises so the durable pending row exists
    by the time the client observes the outcome.
    """
    try:
        from app.gateway.audit_sink import get_audit_sink

        event = build_audit_event(
            action,
            org_id=org_id,
            actor=actor,
            outcome=outcome,
            reason_code=reason_code,
            resource=resource,
            payload=payload or {},
        )
        await get_audit_sink().emit(event)
    except Exception:  # noqa: BLE001 — Class B is best-effort, never a correctness gate
        logger.warning("class-b audit emit failed for action=%s", action, exc_info=True)


__all__ = ["emit_class_b_audit"]
