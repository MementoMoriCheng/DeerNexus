"""Add the ``release_channels`` / ``release_events`` tables (PR-053).

Revision ID: 0013_release_channels_events
Revises: 0012_agent_artifacts
Create Date: 2026-07-26

Track E PR-053 materialises the ADR-0004 §5/§7/§8 channel layer on top of the
PR-050 ``agent_packages`` / ``agent_versions`` tables:

* ``release_channels`` (data-model.md §6.4) — the mutable per-(org, workspace,
  package, channel) pointer whose ``current_version_id`` tracks the version
  that channel currently resolves. Updates go through Compare-And-Swap on
  ``row_version`` (the codebase's first CAS caller — promote/rollback).
* ``release_events`` (data-model.md §6.5) — the append-only domain history of
  every promote / rollback (distinct from the compliance-grade
  ``audit_events`` row written in the same transaction per ADR §14).

This revision is **expand-only / additive**: two new tables + indexes, no
existing table modified, no data backfilled.

Cross-dialect UNIQUE with NULL workspace
-----------------------------------------

ADR §5 mandates one channel pointer per ``(org_id, workspace_id, package_id,
channel)`` even when ``workspace_id IS NULL``. PostgreSQL 15+ expresses this
natively as ``UNIQUE ... NULLS NOT DISTINCT``; SQLite (the test backend) has
no such clause and treats NULLs as distinct under three-valued logic, so a
plain ``UNIQUE`` would allow two rows with ``workspace_id IS NULL``.

The resolution is a **dialect-branched unique INDEX** (not a table-level
``UniqueConstraint``, which cannot be made dialect-conditional):

* PostgreSQL: ``CREATE UNIQUE INDEX ... (org_id, workspace_id, package_id,
  channel) NULLS NOT DISTINCT``
* SQLite (and any other backend): ``CREATE UNIQUE INDEX ... (org_id,
  COALESCE(workspace_id, '_default'), package_id, channel)`` — the COALESCE
  expression collapses NULL to a sentinel so two NULL-workspace rows collide
  on the index. Verified equivalent to NULLS NOT DISTINCT semantics on
  SQLite 3.x.

The ORM model (``release/model.py``) deliberately does NOT declare a
``UniqueConstraint`` for this — ``Base.metadata.create_all`` would emit a
plain UNIQUE that fails to enforce NULL-collision on SQLite, breaking the
``create_all`` ↔ migrated parity that ``test_release_schema.py`` asserts.
The migration is the single source of truth for this constraint.

What this revision does NOT do
------------------------------

* No ``release_channels``/``release_events`` triggers — CAS is enforced by
  the repository (``_cas_update_channel`` checks ``rowcount`` after a
  ``WHERE row_version = :expected`` UPDATE), not a DB trigger (no
  cross-dialect CAS trigger precedent in this codebase).
* No ``ReleaseResolver`` adapter — channel resolution into a ``ReleaseRef``
  is PR-054 (Run admission path). This PR only lands the pointer + history
  tables and the promote/rollback write path.
* No request-level Idempotency-Key replay store — deferred to a follow-up
  (ADR §7 lists it; PR-053 lands only the CAS + conflict path).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.migrations._helpers import safe_create_index, safe_create_table

# revision identifiers, used by Alembic.
revision: str = "0013_release_channels_events"
down_revision: str | Sequence[str] | None = "0012_agent_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The cross-dialect unique index name (shared by both branches so the ORM
#: parity test can look it up by name regardless of backend).
_UNIQUE_INDEX_NAME = "uq_release_channels_org_ws_pkg_channel"


def _create_unique_index(table: str) -> None:
    """Create the NULLS-NOT-DISTINCT-equivalent unique INDEX (dialect-branched).

    See module docstring for the cross-dialect rationale. Both branches target
    the same logical constraint ``(org_id, workspace_id, package_id, channel)``
    with NULL-collision semantics; the index name is identical so downstream
    parity checks (``test_release_schema_channels.py``) find it on either
    backend.
    """
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        # Native Postgres 15+ clause — the production target.
        op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {_UNIQUE_INDEX_NAME} ON {table} (org_id, workspace_id, package_id, channel) NULLS NOT DISTINCT")
    else:
        # SQLite (and any non-Postgres backend): COALESCE collapses NULL to a
        # sentinel so two NULL-workspace rows collide. Verified equivalent to
        # NULLS NOT DISTINCT for the uniqueness purpose.
        op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {_UNIQUE_INDEX_NAME} ON {table} (org_id, COALESCE(workspace_id, '_default'), package_id, channel)")


def upgrade() -> None:
    """Create the ``release_channels`` and ``release_events`` tables (ADR-0004 §5)."""
    # release_channels first — release_events.channel_id references it.
    safe_create_table(
        "release_channels",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.String(length=36), nullable=True),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["agent_packages.id"],
            ondelete="RESTRICT",
            name="fk_release_channels_package_id",
        ),
        sa.ForeignKeyConstraint(
            ["current_version_id"],
            ["agent_versions.id"],
            ondelete="RESTRICT",
            name="fk_release_channels_current_version_id",
        ),
        sa.CheckConstraint(
            "channel IN ('dev', 'staging', 'prod')",
            name="ck_release_channels_channel",
        ),
    )
    # Cross-dialect unique index (NULLS NOT DISTINCT on Postgres; COALESCE on
    # SQLite). NOT a table-level UniqueConstraint — see module docstring.
    _create_unique_index("release_channels")
    safe_create_index("idx_release_channels_lookup", "release_channels", ["org_id", "package_id"])
    safe_create_index("idx_release_channels_org", "release_channels", ["org_id"])

    # release_events — append-only domain history (ADR §14: distinct from the
    # audit_events compliance row written in the same transaction).
    safe_create_table(
        "release_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("channel_id", sa.String(length=36), nullable=False),
        sa.Column("from_version_id", sa.String(length=36), nullable=True),
        sa.Column("to_version_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=True),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["release_channels.id"],
            ondelete="RESTRICT",
            name="fk_release_events_channel_id",
        ),
        sa.CheckConstraint(
            "action IN ('promote', 'rollback')",
            name="ck_release_events_action",
        ),
    )
    safe_create_index("idx_release_events_channel", "release_events", ["channel_id"])
    safe_create_index("idx_release_events_org", "release_events", ["org_id"])


def downgrade() -> None:
    """Drop the two tables (reverse FK / index order: events before channels)."""
    op.drop_index("idx_release_events_org", table_name="release_events")
    op.drop_index("idx_release_events_channel", table_name="release_events")
    op.drop_table("release_events")
    op.drop_index("idx_release_channels_org", table_name="release_channels")
    op.drop_index("idx_release_channels_lookup", table_name="release_channels")
    op.execute(f"DROP INDEX IF EXISTS {_UNIQUE_INDEX_NAME}")
    op.drop_table("release_channels")
