"""Append-only data-access layer for the ``audit_events`` table (PR-040).

This module is the **only** write path for audit evidence in the harness
layer. It exposes INSERT + SELECT helpers and **nothing else**: there is no
``update_audit_event`` and no ``delete_audit_event``. Append-only is the
table's defining invariant (ADR-0005 §10.1, §13), and the absence of a
mutation entry point makes it impossible to write code that edits or erases
a recorded event — corrections append a NEW ``audit.event.corrected`` row
referencing ``original_event_id`` in payload (§13), the original is never
touched.

Defence-in-depth at the DB layer: migration ``0010`` installs a
``BEFORE UPDATE OR DELETE`` trigger that aborts any such statement, so even
a caller that bypasses this repository (raw SQL, a future script) cannot
mutate the table. The in-app INSERT-only surface and the DB trigger together
realise ADR-0005 §10.1's "无 UPDATE / DELETE" for the single-connection
harness; role-based ``GRANT``/``REVOKE`` privilege isolation (§16 step 2)
is deferred to the ops runbook because the harness connects via one owner
DSN with no role-provisioning machinery today.

The producer of an event is the app layer (the Class A control-plane write
paths that PR-042 wires up, and the outbox worker PR-041 introduces). This
module is deliberately payload-agnostic: it persists whatever
``AuditEvent`` the caller hands it, scrubbing secret-bearing keys via
``contracts.events._scrub_payload`` as belt-and-braces (the DTO already
rejects forbidden keys at construction, but a caller that builds a row
directly must not be able to leak a credential into the store).

Conventions (mirror ``persistence.iam.repository``): each function opens
its own ``AsyncSession`` from the supplied ``async_sessionmaker``; writes
commit before returning; reads always apply an explicit ``org_id`` filter
for tenant queries (ADR §8 / §12.1 — a cross-Org read is a system-admin
operation that must go through a separate, audited path).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.contracts.events import AuditEvent, _scrub_payload
from deerflow.persistence.audit.model import AUDIT_OUTCOMES, AuditEventRow

#: Maximum rows returned by a single ``list_audit_events`` call (ADR-0005
#: §12.1: cursor pagination, bounded page size). A larger window is the
#: async export job's responsibility (PR-045), not the online query path.
DEFAULT_PAGE_SIZE = 100


def _event_to_row(
    event: AuditEvent,
    *,
    producer: str | None,
    producer_version: str | None,
    partition_key: str | None,
) -> AuditEventRow:
    """Project an ``AuditEvent`` DTO onto an ``AuditEventRow`` (lossless).

    ``actor`` and ``resource`` are flattened into indexable columns rather
    than stored as nested JSON, so the §12.1 query dimensions (actor,
    resource type/id) stay indexable. The round-trip is lossless: the
    original ``PrincipalRef`` / ``ResourceRef`` can be reconstructed from
    the flattened columns. ``payload`` is scrubbed a second time here as
    defence-in-depth — the DTO rejects forbidden keys at construction, but
    a caller that built the DTO via ``model_construct`` (skipping
    validation) must still not leak a secret into the store.
    """
    actor = event.actor
    resource = event.resource
    return AuditEventRow(
        event_id=event.event_id,
        idempotency_key=event.idempotency_key,
        schema_version=event.schema_version,
        org_id=event.org_id,
        workspace_id=event.workspace_id,
        actor_type=actor.type,
        actor_id=actor.id,
        actor_user_id=actor.user_id,
        actor_display_name=actor.display_name,
        action=event.action,
        outcome=event.outcome,
        reason_code=event.reason_code,
        resource_type=resource.type if resource is not None else None,
        resource_id=resource.id if resource is not None else None,
        resource_org_id=resource.org_id if resource is not None else None,
        resource_workspace_id=resource.workspace_id if resource is not None else None,
        resource_attributes=resource.attributes if resource is not None else None,
        request_id=event.request_id,
        trace_id=event.trace_id,
        run_id=event.run_id,
        occurred_at=event.occurred_at,
        payload=_scrub_payload(dict(event.payload)),
        producer=producer,
        producer_version=producer_version,
        partition_key=partition_key,
    )


async def insert_audit_event(
    sf: async_sessionmaker[AsyncSession],
    event: AuditEvent,
    *,
    producer: str | None = None,
    producer_version: str | None = None,
    partition_key: str | None = None,
) -> AuditEventRow:
    """Persist one ``AuditEvent`` (append-only INSERT).

    ``event_id`` is the primary key; a duplicate insert raises
    ``IntegrityError`` (mapped to a no-op retry by the outbox worker in
    PR-041, ADR §9.1 idempotency by ``event_id``). This function performs
    exactly one INSERT — there is no UPDATE fallback and no delete path
    anywhere in this module (see module docstring for the append-only
    guarantee).

    The caller supplies persistence-extras (``producer`` /
    ``producer_version`` / ``partition_key``, ADR-0005 §3 "持久化额外
    记录"); ``ingested_at`` defaults to now inside the ORM.
    """
    if event.outcome not in AUDIT_OUTCOMES:
        # Belt-and-braces: the DTO + CHECK constraint already enforce this,
        # but fail fast with a clear message before hitting the DB.
        raise ValueError(f"audit event outcome must be one of {AUDIT_OUTCOMES}, got {event.outcome!r}")
    row = _event_to_row(
        event,
        producer=producer,
        producer_version=producer_version,
        partition_key=partition_key,
    )
    async with sf() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def get_audit_event(
    sf: async_sessionmaker[AsyncSession],
    *,
    event_id: str,
) -> AuditEventRow | None:
    """Return the row for ``event_id`` or ``None``.

    No ``org_id`` scoping: a lookup by primary key is an internal/admin
    operation. Org-scoped reads MUST go through ``list_audit_events`` which
    enforces the org filter (ADR §8 / §12.1).
    """
    async with sf() as session:
        return await session.get(AuditEventRow, event_id)


async def count_by_org(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
) -> int:
    """Count events for ``org_id``. ``org_id`` is required (no system scan)."""
    async with sf() as session:
        stmt = select(func.count()).select_from(AuditEventRow).where(AuditEventRow.org_id == org_id)
        return int((await session.execute(stmt)).scalar_one())


async def list_audit_events(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    action: str | None = None,
    actor_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    outcome: str | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
    cursor: tuple[datetime, str] | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[AuditEventRow]:
    """Org-scoped audit query with cursor pagination (ADR-0005 §12.1).

    ``org_id`` is **required**: the online query path must force Org
    (ADR §8 / §12.1 "强制 Org"); a cross-Org system-admin query is a
    separate, separately-audited path and does not call this helper.

    Filters mirror the §12.1 allow-list: action, actor, resource type/id,
    outcome, run_id, request_id, and a time range. Pagination uses a
    ``(occurred_at, event_id)`` cursor — the stable single-resource sort
    order from §9.2 (occurred_at alone is not unique; event_id breaks
    ties deterministically). Pass the last row's ``(occurred_at, event_id)``
    as ``cursor`` to fetch the next page; pass ``None`` for the first page.

    The page size is capped at :data:`DEFAULT_PAGE_SIZE`; the async export
    job (PR-045) handles unbounded windows, not this online path.
    """
    if limit <= 0 or limit > DEFAULT_PAGE_SIZE:
        limit = DEFAULT_PAGE_SIZE

    async with sf() as session:
        stmt = select(AuditEventRow).where(AuditEventRow.org_id == org_id)
        if action is not None:
            stmt = stmt.where(AuditEventRow.action == action)
        if actor_id is not None:
            stmt = stmt.where(AuditEventRow.actor_id == actor_id)
        if resource_type is not None:
            stmt = stmt.where(AuditEventRow.resource_type == resource_type)
        if resource_id is not None:
            stmt = stmt.where(AuditEventRow.resource_id == resource_id)
        if outcome is not None:
            stmt = stmt.where(AuditEventRow.outcome == outcome)
        if run_id is not None:
            stmt = stmt.where(AuditEventRow.run_id == run_id)
        if request_id is not None:
            stmt = stmt.where(AuditEventRow.request_id == request_id)
        if occurred_after is not None:
            stmt = stmt.where(AuditEventRow.occurred_at > occurred_after)
        if occurred_before is not None:
            stmt = stmt.where(AuditEventRow.occurred_at < occurred_before)
        if cursor is not None:
            cursor_at, cursor_id = cursor
            # Strictly-after the cursor on the (occurred_at, event_id) order.
            stmt = stmt.where((AuditEventRow.occurred_at > cursor_at) | ((AuditEventRow.occurred_at == cursor_at) & (AuditEventRow.event_id > cursor_id)))
        # Stable cursor order: occurred_at ASC, event_id ASC tie-break (§9.2).
        stmt = stmt.order_by(AuditEventRow.occurred_at.asc(), AuditEventRow.event_id.asc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
    return list(rows)
