"""Temporary tenant audit-event interface (PR-022).

This module is the **explicit, non-silent** event sink mandated by
``pr-split-guide.md`` §7 (PR-022): "Audit outbox 依赖尚未合并时使用明确临时
事件接口，不静默丢失" — while the Audit outbox dependency (PR-041) is not yet
merged, tenant-lifecycle events (default Org creation, admin membership /
role-binding creation) must still be recorded somewhere observable rather than
silently dropped.

Current implementation: structured ``logger.info``. This is deliberately a
single choke-point so PR-041 can swap the body for an outbox write (append to
``audit_events`` table / message bus) without touching call sites.

Contract: this function MUST NOT raise on a logging failure (events are
best-effort observability, never a correctness gate), and MUST NOT silently
no-op — at minimum the event is logged at INFO level.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


def emit_tenant_event(
    event_type: str,
    *,
    org_id: str | None,
    principal_id: str | None,
    payload: Mapping[str, Any] | None = None,
) -> None:
    """Record a tenant-lifecycle event (temporary logger sink).

    Args:
        event_type: Stable event identifier (e.g. ``"default_org_created"``,
            ``"admin_membership_created"``).
        org_id: Organization the event concerns (``None`` for system-template
            events that are not org-scoped).
        principal_id: Principal the event concerns (typically the admin user
            id; ``None`` when not principal-scoped).
        payload: Optional structured details.

    Replaced by the Audit outbox write in PR-041; until then every event is
    logged so none is silently lost.
    """
    logger.info(
        "tenant-event type=%s org=%s principal=%s payload=%s",
        event_type,
        org_id,
        principal_id,
        dict(payload) if payload else {},
    )
