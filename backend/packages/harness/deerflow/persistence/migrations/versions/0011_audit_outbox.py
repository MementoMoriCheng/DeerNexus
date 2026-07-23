"""Create the ``audit_outbox`` transactional-outbox table (PR-041).

Revision ID: 0011_audit_outbox
Revises: 0010_audit_events
Create Date: 2026-07-23

Track D PR-041 lands the reliable queue that drains into the append-only
``audit_events`` store (PR-040). The outbox is ADR-0005 §8: a Class A
control-plane write enqueues a row (same transaction in PR-042), and a
background worker claims, publishes to ``audit_events``, then marks the row
``published`` (or ``dead_letter`` after the retry threshold).

This revision is **expand-only / additive**: it creates one new table and its
indexes. No existing table is modified and no data is backfilled.

Schema parity with ``Base.metadata``
------------------------------------

The table mirrors ``deerflow.persistence.audit.model.AuditOutboxRow`` exactly
so a fresh DB (provisioned by ``create_all`` + ``stamp head``) and a legacy-
upgraded DB are schema-identical. Uses ``safe_create_table`` /
``safe_create_index`` (the idempotent helpers from PR-020A) so the full
table+index revision is re-runnable against a DB the legacy branch's
``create_all`` has already seeded.

Unlike ``audit_events`` (PR-040), this table has NO append-only trigger:
the outbox row has a legitimate status transition
(``pending → processing → published | dead_letter``), so UPDATE is required
and expected. The append-only guarantee belongs to ``audit_events`` (the
immutable published record), not the transient queue.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.migrations._helpers import safe_create_index, safe_create_table

# revision identifiers, used by Alembic.
revision: str = "0011_audit_outbox"
down_revision: str | Sequence[str] | None = "0010_audit_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``audit_outbox`` table (ADR-0005 §8)."""
    safe_create_table(
        "audit_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("owner_token", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'dead_letter')",
            name="ck_audit_outbox_status",
        ),
        sa.UniqueConstraint("event_id", name="uq_audit_outbox_event_id"),
    )
    # Claim path: workers SELECT ... WHERE status='pending' AND available_at<=now.
    safe_create_index("idx_audit_outbox_claim", "audit_outbox", ["status", "available_at"])
    safe_create_index("idx_audit_outbox_org", "audit_outbox", ["org_id"])


def downgrade() -> None:
    """Drop the ``audit_outbox`` table (reverse index order)."""
    op.drop_index("idx_audit_outbox_org", table_name="audit_outbox")
    op.drop_index("idx_audit_outbox_claim", table_name="audit_outbox")
    op.drop_table("audit_outbox")
