"""Add ``audit_archive_watermarks`` table for the audit archive job (T1C).

Revision ID: 0021_audit_archive_watermarks
Revises: 0020_contract_org_scope
Create Date: 2026-08-13

ADR-0005 §10.2: the daily archive job exports closed-window audit events to
object storage (JSONL), writes a manifest, and records the watermark so the
next sweep knows where to resume. This revision adds the watermark table —
operational metadata for the archive job, NOT an append-only audit event, so
it can be freely UPDATEd (unlike ``audit_events`` which has a
``BEFORE UPDATE OR DELETE`` trigger).

The ``audit_events.archive_batch_id`` column already exists (migration 0010)
but the archive job does not UPDATE it directly because the trigger blocks
that. Instead the watermark lives here; a future DBA-role bypass (ADR §16
step 2) can backfill ``archive_batch_id`` on the event rows from these
watermarks.

Schema parity with ``Base.metadata``
------------------------------------

The ORM model ``AuditArchiveWatermarkRow`` (``audit/watermark_model.py``)
declares the same columns so a fresh DB provisioned by ``create_all`` +
``stamp head`` and a legacy-upgraded DB are schema-identical (guarded by
``test_create_all_and_alembic_upgrade_produce_same_schema``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from deerflow.persistence.migrations._helpers import safe_create_table

# revision identifiers, used by Alembic.
revision: str = "0021_audit_archive_watermarks"
down_revision: str | Sequence[str] | None = "0020_contract_org_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``audit_archive_watermarks`` table."""
    safe_create_table(
        "audit_archive_watermarks",
        sa.Column("org_id", sa.String(length=36), primary_key=True),
        sa.Column("last_archived_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_id", sa.String(length=128), nullable=False),
        sa.Column("batch_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Drop the ``audit_archive_watermarks`` table."""
    from alembic import op

    op.drop_table("audit_archive_watermarks")
