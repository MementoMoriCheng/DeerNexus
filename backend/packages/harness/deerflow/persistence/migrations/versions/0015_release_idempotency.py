"""Add the ``release_idempotency_records`` replay store (PR-055).

Revision ID: 0015_release_idempotency
Revises: 0014_catalog_entries
Create Date: 2026-07-26

Track E PR-055 lands ADR-0004 §7 Idempotency-Key replay semantics on top of
the CAS promote/rollback path delivered in PR-053. The store lets a client
retries a promote/rollback safely: the **same** ``(Idempotency-Key, request)``
pair returns the original result (no second ``_move_channel`` and no second
``release.agent.published`` / ``release.agent.rolled_back`` audit row); a same
key with a **different** request returns ``409 idempotency_conflict``.

This is the Stripe-style "store the full response" shape: the serialized
``PromoteResponse`` (channel + event) is persisted alongside a ``request_hash``
of the semantically-meaningful request fields, so a replay can return the
exact original result and status without re-resolving the (possibly since
moved) channel pointer. The channel's ``row_version`` lives inside the stored
``PromoteResponse.channel.row_version``, so the replay also re-emits a correct
``ETag`` header.

This revision is **expand-only / additive**: one new table + indexes, no
existing table modified, no data backfilled.

Schema notes
------------

* ``UNIQUE(org_id, idempotency_key)`` is a plain constraint (no nullable
  column participates) — no NULLS NOT DISTINCT / COALESCE dance is needed,
  unlike ``release_channels`` (PR-053). The uniqueness is the concurrency
  fence: two concurrent same-key requests cannot both insert; the loser sees
  ``IntegrityError``, rolls back, re-selects and either replays (identical
  request) or surfaces ``idempotency_conflict`` (different request).
* **No FK** on ``org_id`` or any resource id — a replay record is
  self-contained (it carries its own response snapshot). Replaying does not
  re-touch ``release_channels`` / ``release_events``; FK would couple GC of
  replay records to channel lifecycle, which is not the intent.
* **No TTL / ``expires_at`` column** — MVP keeps replay records indefinitely.
  Pruning old records is a follow-up (a maintenance job keyed on
  ``created_at``); adding the column later is additive.

What this revision does NOT do
------------------------------

* No pruning / GC of replay records — follow-up.
* No change to ``release_channels`` / ``release_events`` — the CAS path and
  history table from PR-053 are unchanged; this store sits beside them.
* No Run-pin — ``runs`` is unchanged (Run-pin is a separate PR).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.migrations._helpers import safe_create_index, safe_create_table

# revision identifiers, used by Alembic.
revision: str = "0015_release_idempotency"
down_revision: str | Sequence[str] | None = "0014_catalog_entries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``release_idempotency_records`` replay store (ADR-0004 §7)."""
    safe_create_table(
        "release_idempotency_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        # Client-supplied replay key (Idempotency-Key header). Unique per Org.
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        # sha256 hex of the canonicalized request identity (see
        # ``_request_fingerprint`` in release/idempotency.py). Two requests are
        # "the same" iff this hash matches.
        sa.Column("request_hash", sa.String(length=128), nullable=False),
        # Serialized PromoteResponse (channel + event). Replayed verbatim.
        sa.Column("response_payload", sa.JSON(), nullable=False),
        # Original HTTP status (200 for promote/rollback). Replayed verbatim
        # so a future non-200 path (e.g. 202 accepted) stays correct.
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_release_idempotency_org_key",
        ),
    )
    safe_create_index("idx_release_idempotency_org", "release_idempotency_records", ["org_id"])
    safe_create_index(
        "idx_release_idempotency_org_key",
        "release_idempotency_records",
        ["org_id", "idempotency_key"],
    )


def downgrade() -> None:
    """Drop the ``release_idempotency_records`` table (reverse index order)."""
    op.drop_index("idx_release_idempotency_org_key", table_name="release_idempotency_records")
    op.drop_index("idx_release_idempotency_org", table_name="release_idempotency_records")
    op.drop_table("release_idempotency_records")
