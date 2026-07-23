"""Create the append-only ``audit_events`` table (PR-040).

Revision ID: 0010_audit_events
Revises: 0009_oidc_group_mappings
Create Date: 2026-07-23

Track D PR-040 lands the stable compliance-evidence storage described by
ADR-0005 §3/§10 and runtime-contracts.md §10: an append-only record of
who did what to which Org's resource and with what outcome. This revision
is **expand-only / additive**: it creates one new table and its indexes,
plus a ``BEFORE UPDATE OR DELETE`` trigger that enforces append-only at
the DB layer. No existing table is modified and no data is backfilled —
the table is empty until the outbox worker (PR-041) and the Class A write
paths (PR-042) start producing events.

Schema parity with ``Base.metadata``
------------------------------------

The table mirrors ``deerflow.persistence.audit.model.AuditEventRow``
exactly so a fresh DB (provisioned by ``create_all`` + ``stamp head``)
and a legacy-upgraded DB are schema-identical (columns / nullability /
CHECK). Uses ``safe_create_table`` / ``safe_create_index`` (the idempotent
helpers from PR-020A) so the full table+index revision is re-runnable
against a DB the legacy branch's ``create_all`` has already seeded.

Append-only trigger (defence-in-depth, ADR-0005 §10.1 / §13)
-------------------------------------------------------------

The trigger is created ONLY by this migration, NOT by the ORM
``create_all`` path (DDL triggers are not part of SQLAlchemy metadata).
This is acceptable and intentional:

* The parity test (``test_create_all_and_alembic_upgrade_produce_same_schema``)
  compares tables / columns / nullability / server_default only — it does
  not inspect triggers, so the asymmetric trigger does not break parity.
* The in-app guarantee (``persistence.audit.repository`` exposes INSERT +
  SELECT only, no UPDATE/DELETE) is the primary enforcement on ALL paths,
  including the dev ``create_all`` path. The DB trigger is belt-and-braces
  for migrated/prod DBs where a caller might bypass the repository.

Role-based ``GRANT``/``REVOKE`` privilege isolation (ADR-0005 §16 step 2)
is deferred to the ops runbook: the harness connects via a single owner
DSN with no role-provisioning machinery today, so a trigger is the only
dialect-portable, CI-testable way to make the append-only guarantee real
at the DB layer right now.

Cross-dialect note: SQLite and Postgres trigger syntax differ. The
``upgrade()`` inspects ``op.get_bind().dialect.name`` and emits the
appropriate DDL. SQLite raises ``ABORT``; Postgres raises an exception
via a trigger function.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.audit.model import AUDIT_OUTCOMES
from deerflow.persistence.migrations._helpers import safe_create_index, safe_create_table

# revision identifiers, used by Alembic.
revision: str = "0010_audit_events"
down_revision: str | Sequence[str] | None = "0009_oidc_group_mappings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Name of the BEFORE UPDATE OR DELETE trigger + its Postgres guard function.
_TRIGGER_NAME = "trg_audit_events_append_only"
_FUNC_NAME = "fn_audit_events_append_only_guard"


def _create_append_only_trigger() -> None:
    """Install the ``BEFORE UPDATE OR DELETE`` abort trigger (dialect-aware).

    On SQLite, a single statement-level trigger with ``FOR EACH STATEMENT``
    is not needed (SQLite triggers are row-level by default); the
    ``RAISE(ABORT, ...)`` cancels the offending statement. On Postgres a
    trigger function raises an exception, attached to both UPDATE and
    DELETE as ``BEFORE`` row-level triggers.
    """
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        # SQLite: RAISE(ABORT, ...) cancels the statement and surfaces the
        # message as the error string. Separate triggers for UPDATE / DELETE
        # (SQLite has no single OR-trigger form).
        op.execute(
            f"""
            CREATE TRIGGER {_TRIGGER_NAME}_update BEFORE UPDATE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only: UPDATE rejected');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_TRIGGER_NAME}_delete BEFORE DELETE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only: DELETE rejected');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_FUNC_NAME}() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'audit_events is append-only: % rejected', TG_OP;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_TRIGGER_NAME} BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION {_FUNC_NAME}()
            """
        )
    # Other dialects: the in-app INSERT-only repository is the guarantee;
    # no DB trigger is installed. (The test suite exercises SQLite.)


def _drop_append_only_trigger() -> None:
    """Remove the trigger + Postgres function (reverse of create)."""
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}_update")
        op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}_delete")
    elif dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON audit_events")
        op.execute(f"DROP FUNCTION IF EXISTS {_FUNC_NAME}()")


def upgrade() -> None:
    """Create the ``audit_events`` table, indexes, and append-only trigger."""
    safe_create_table(
        "audit_events",
        # Identity (§3.1)
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        # Scope
        sa.Column("org_id", sa.String(length=36), nullable=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        # Actor (flattened)
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_display_name", sa.String(length=256), nullable=True),
        # Action / outcome
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        # Resource (flattened)
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=36), nullable=True),
        sa.Column("resource_org_id", sa.String(length=36), nullable=True),
        sa.Column("resource_workspace_id", sa.String(length=36), nullable=True),
        sa.Column("resource_attributes", sa.JSON(), nullable=True),
        # Trace context
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        # Time / body
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        # Persistence extras (§3)
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("producer", sa.String(length=128), nullable=True),
        sa.Column("producer_version", sa.String(length=64), nullable=True),
        sa.Column("partition_key", sa.String(length=128), nullable=True),
        sa.Column("archive_batch_id", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
        sa.CheckConstraint(
            f"outcome IN {AUDIT_OUTCOMES!r}",
            name="ck_audit_events_outcome",
        ),
    )
    # Query indexes (ADR-0005 §10.1 / §12.1 filter dimensions).
    safe_create_index("idx_audit_events_org_time", "audit_events", ["org_id", "occurred_at", "event_id"])
    safe_create_index("idx_audit_events_action", "audit_events", ["action"])
    safe_create_index("idx_audit_events_actor", "audit_events", ["actor_type", "actor_id"])
    safe_create_index("idx_audit_events_resource", "audit_events", ["resource_type", "resource_id"])
    safe_create_index("idx_audit_events_request_id", "audit_events", ["request_id"])
    safe_create_index("idx_audit_events_idempotency_key", "audit_events", ["idempotency_key"])

    # Append-only guard trigger (defence-in-depth beyond the in-app
    # INSERT-only repository).
    _create_append_only_trigger()


def downgrade() -> None:
    """Drop trigger, indexes, and the ``audit_events`` table (reverse order)."""
    _drop_append_only_trigger()
    op.drop_index("idx_audit_events_idempotency_key", table_name="audit_events")
    op.drop_index("idx_audit_events_request_id", table_name="audit_events")
    op.drop_index("idx_audit_events_resource", table_name="audit_events")
    op.drop_index("idx_audit_events_actor", table_name="audit_events")
    op.drop_index("idx_audit_events_action", table_name="audit_events")
    op.drop_index("idx_audit_events_org_time", table_name="audit_events")
    op.drop_table("audit_events")
