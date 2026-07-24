"""Audit-evidence ORM model and append-only repository (PR-040).

Re-exports the ``AuditEventRow`` so ``deerflow.persistence.models`` can
register it with ``Base.metadata`` in a single import, and the
INSERT-only repository helpers so the app layer (PR-041 outbox worker,
PR-042 Class A write paths) can persist audit evidence without importing
``persistence.audit.repository`` directly.

There is intentionally NO update/delete helper here — the ``audit_events``
table is append-only (ADR-0005 §10.1, §13); see ``repository.py``'s module
docstring for the in-app + DB-trigger enforcement.
"""

from deerflow.persistence.audit.model import AUDIT_OUTCOMES, AuditEventRow, AuditOutboxRow
from deerflow.persistence.audit.outbox import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
    DEAD_LETTER_THRESHOLD,
    OUTBOX_DEAD_LETTER,
    OUTBOX_PENDING,
    OUTBOX_PROCESSING,
    OUTBOX_PUBLISHED,
    STALE_PROCESSING_SECONDS,
    claim_audit_outbox,
    count_dead_letter,
    count_pending,
    enqueue_audit_outbox,
    enqueue_audit_outbox_in_session,
    mark_outbox_failed,
    mark_outbox_published,
    oldest_pending_age_seconds,
    release_stale_processing,
)
from deerflow.persistence.audit.repository import (
    DEFAULT_PAGE_SIZE,
    count_by_org,
    get_audit_event,
    insert_audit_event,
    list_audit_events,
)

__all__ = [
    # ORM models
    "AUDIT_OUTCOMES",
    "AuditEventRow",
    "AuditOutboxRow",
    # repository (PR-040, append-only audit_events)
    "DEFAULT_PAGE_SIZE",
    "count_by_org",
    "get_audit_event",
    "insert_audit_event",
    "list_audit_events",
    # outbox queue lifecycle (PR-041, audit_outbox)
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_MAX_SECONDS",
    "DEAD_LETTER_THRESHOLD",
    "OUTBOX_DEAD_LETTER",
    "OUTBOX_PENDING",
    "OUTBOX_PROCESSING",
    "OUTBOX_PUBLISHED",
    "STALE_PROCESSING_SECONDS",
    "claim_audit_outbox",
    "count_dead_letter",
    "count_pending",
    "enqueue_audit_outbox",
    "enqueue_audit_outbox_in_session",
    "mark_outbox_failed",
    "mark_outbox_published",
    "oldest_pending_age_seconds",
    "release_stale_processing",
]
