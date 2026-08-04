"""Add the ``model_providers`` table for user-owned custom model suppliers.

Revision ID: 0019_model_providers
Revises: 0018_run_cancel_intent
Create Date: 2026-08-04

Lands per-user custom model-provider persistence. Each row is a private,
user-owned model supplier (OpenAI-compatible endpoint + encrypted API key)
that the per-request config-merge middleware injects into the running
``AppConfig.models`` list so the user can pick and actually invoke it in chat.

This revision is **expand-only / additive**: one new table + indexes. No
existing table is modified and no data is backfilled — the table is empty
until the write path (the ``/api/model-providers`` CRUD router in the same
PR) populates it.

Schema notes
------------

* ``owner_user_id`` scopes every row to a single user — the source of the
  per-user isolation. ``UNIQUE(owner_user_id, name)`` prevents a user from
  registering two suppliers with the same identifier; two different users may
  reuse the same ``name``.
* ``encrypted_api_key`` holds a Fernet ciphertext (``fernet:v1:...``). The
  cleartext key never reaches the DB; the ``ChannelCredentialCipher`` reused
  from ``channel_connections`` does the encrypt/decrypt in the repository.
* No FK on ``owner_user_id`` — mirrors the IAM polymorphic-principal
  convention (no FK); identity/integrity is enforced by the write path and
  the auth middleware that stamps ``request.state.user``.

What this revision does NOT do
------------------------------

* No config-merge middleware — that lands in a follow-up PR which reads this
  table per-request and merges rows into the request-scoped ``AppConfig``.
* No ``org_id`` / sharing — this track is strictly per-user private; an
  org-level sharing layer would be a separate table/PR.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.migrations._helpers import (
    safe_create_index,
    safe_create_table,
    safe_drop_index,
)

# revision identifiers, used by Alembic.
revision: str = "0019_model_providers"
down_revision: str | Sequence[str] | None = "0018_run_cancel_intent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``model_providers`` table."""
    safe_create_table(
        "model_providers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("use", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("supports_thinking", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("supports_reasoning_effort", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_user_id", "name", name="uq_model_providers_owner_name"),
    )
    safe_create_index("idx_model_providers_owner", "model_providers", ["owner_user_id"])


def downgrade() -> None:
    """Drop the ``model_providers`` table (reverse index order).

    The index drop is guarded by ``safe_drop_index`` because, under the
    bootstrap's legacy branch, the table is seeded by ``create_all``
    (which reflects the ORM ``__table_args__`` but NOT migration-only
    indexes like ``idx_model_providers_owner``). Dropping by name then
    fails with ``no such index``; the safe helper no-ops instead.
    """
    safe_drop_index("idx_model_providers_owner", "model_providers")
    op.drop_table("model_providers")
