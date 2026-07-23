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

from deerflow.persistence.audit.model import AUDIT_OUTCOMES, AuditEventRow
from deerflow.persistence.audit.repository import (
    DEFAULT_PAGE_SIZE,
    count_by_org,
    get_audit_event,
    insert_audit_event,
    list_audit_events,
)

__all__ = [
    # ORM model
    "AUDIT_OUTCOMES",
    "AuditEventRow",
    # repository (PR-040)
    "DEFAULT_PAGE_SIZE",
    "count_by_org",
    "get_audit_event",
    "insert_audit_event",
    "list_audit_events",
]
