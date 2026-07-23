"""ORM model for the append-only ``audit_events`` compliance table (PR-040).

This table is the storage substrate described by ADR-0005 §3/§10 and
runtime-contracts.md §10: a stable, append-only record of who did what to
which Org's resource and with what outcome. It is **not** a RunEvent (which
serves run debugging) and **not** a log line (which may be sampled/expired).

Design points (ADR-0005 §3, §3.1):

* ``event_id`` is the global primary key (§3.1 invariant). Retries reuse the
  same ``event_id``; the store deduplicates on it (§9.1).
* ``idempotency_key`` is producer-stable and supports domain-level duplicate
  detection — it is **indexed but not globally unique** (§9.1).
* ``org_id`` is nullable at the column level because system-global events
  (``builtin_role_created``, ``system.break_glass.enabled``) carry ``None``;
  the "tenant events require org_id" rule is an app/service-layer invariant
  (ADR-0005 §3.1), not expressible as a column constraint.
* ``actor`` / ``resource`` are **flattened** (``actor_type`` /
  ``actor_id`` / ``actor_user_id`` / ``actor_display_name`` and
  ``resource_*``) rather than stored as nested JSON: this keeps the query
  dimensions ADR-0005 §12.1 requires (actor, resource type/id) indexable
  without a JSON-path expression, and the round-trip is lossless against the
  ``AuditEvent`` DTO (``contracts.events.AuditEvent``).
* ``occurred_at`` is when the event happened; ``ingested_at`` is when the
  store received it (§3.1 / §9.3). The two are distinct so late-arriving
  events keep their original ``occurred_at`` while still being identifiable
  as delayed.
* ``payload`` is free-form JSON but MUST be scrubbed of secret-bearing keys
  before persistence (§6); the repository reuses
  ``contracts.events._scrub_payload`` defence-in-depth so a careless producer
  cannot leak a credential into the store even if it places one in the dict.

Append-only enforcement (ADR-0005 §10.1, §13)
--------------------------------------------

This table has **no** ``created_at``/``updated_at``/``row_version`` columns:
an audit event is immutable, so there is no UPDATE path to timestamp.
Append-only is enforced on two layers:

1. **In-app (all code paths):** ``persistence.audit.repository`` exposes only
   INSERT and SELECT helpers — there is no UPDATE/DELETE function to call.
2. **Database trigger (defence-in-depth, migrated/prod DBs):** migration
   ``0010_audit_events`` installs a ``BEFORE UPDATE OR DELETE`` trigger that
   aborts the statement regardless of which role issues it. A trigger
   (rather than ``GRANT``/``REVOKE`` privilege isolation) is used because
   the harness connects via a single owner DSN with no role-provisioning
   machinery today; full role-based privilege isolation (ADR-0005 §16 step
   2) is deferred to the ops runbook. The trigger makes the guarantee
   dialect-portable and CI-testable.

Cross-backend conventions match the other control-plane tables (see
``iam/model.py``): ``JSON`` (not ``JSONB``), ``DateTime(timezone=True)``,
``String(36)`` UUIDs for id/reference columns.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base

#: Allowed values for ``outcome`` (ADR-0005 §3, AuditOutcome Literal).
# Kept as a module constant so the repository / tests can reference the
# closed set without importing the pydantic Literal.
AUDIT_OUTCOMES = ("success", "denied", "failure")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditEventRow(Base):
    """Append-only compliance evidence row (ADR-0005 §3).

    One row == one ``AuditEvent``. Inserted exactly once (deduplicated by
    ``event_id``); never updated, never deleted (see module docstring for
    the trigger / in-app append-only guarantee). Corrections append a NEW
    ``audit.event.corrected`` row referencing ``original_event_id`` in
    payload — the original is untouched (ADR-0005 §13).
    """

    __tablename__ = "audit_events"

    # --- Identity (ADR-0005 §3.1) -----------------------------------------
    # Global primary key. Retries reuse it; the store deduplicates on it.
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Producer-stable idempotency key (§9.1). Indexed for domain-level
    # duplicate detection but NOT globally unique.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v1alpha1")

    # --- Scope ------------------------------------------------------------
    # Nullable for system-global events (builtin_role_created, system.*).
    # "tenant events require org_id" is a service-layer invariant.
    org_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # --- Actor (flattened from PrincipalRef, lossless) --------------------
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # actor_user_id only set for user principals (PrincipalRef.user_id).
    actor_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # --- Action / outcome -------------------------------------------------
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- Resource (flattened from ResourceRef | None, lossless) -----------
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resource_org_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resource_workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resource_attributes: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- Trace context ----------------------------------------------------
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # --- Time / body ------------------------------------------------------
    # occurred_at = when the event happened; ingested_at = when stored.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # --- Persistence extras (ADR-0005 §3 "持久化额外记录") ----------------
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    producer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    producer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    partition_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    archive_batch_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"outcome IN {AUDIT_OUTCOMES!r}",
            name="ck_audit_events_outcome",
        ),
        # §10.1 "查询索引包含 Org、时间、Action、Resource" + §12.1 filter
        # dimensions. (occurred_at, event_id) is the stable cursor order
        # for single-resource history (§9.2).
        Index("idx_audit_events_org_time", "org_id", "occurred_at", "event_id"),
        Index("idx_audit_events_action", "action"),
        Index("idx_audit_events_actor", "actor_type", "actor_id"),
        Index("idx_audit_events_resource", "resource_type", "resource_id"),
        Index("idx_audit_events_request_id", "request_id"),
        Index("idx_audit_events_idempotency_key", "idempotency_key"),
    )
