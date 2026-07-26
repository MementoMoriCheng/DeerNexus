"""Add the ``catalog_entries`` discovery-index table (PR-054).

Revision ID: 0014_catalog_entries
Revises: 0013_release_channels_events
Create Date: 2026-07-26

Track E PR-054 lands the ADR-0004 §10 / data-model.md §6.6 catalog discovery
index. The Catalog is a **cross-resource discovery index** (agents + skills +
mcp + tools) keyed by ``(org_id, resource_type, resource_id)`` — distinct from
the ``Manifest.source_metadata`` provenance field on ``agent_versions``
(PR-051/052), which records per-Version import provenance. The Catalog lets
operators browse what exists in their Org; execution authority remains the
``ReleaseRef`` (resolved by the ``DbReleaseResolver`` adapter landed alongside
this table).

This revision is **expand-only / additive**: one new table + indexes. No
existing table is modified and no data is backfilled — the table is empty
until the write path (import / promote projecting into the Catalog) lands in
a follow-up. ``GET /catalog`` therefore returns an empty list initially.

Schema notes
------------

* No FK on ``resource_id`` — ADR §10 "Catalog 是发现索引,不是执行权威" (the
  Catalog is a discovery index, not execution authority). ``resource_id`` is
  a soft reference; integrity is the writer's responsibility.
* Three CHECKs close the ``resource_type`` (agent/skill/mcp/tool), ``source``
  (database/file_import/system), and ``status`` (active/disabled/archived)
  sets so a malformed row fails at insert, not at read.
* The UNIQUE ``(org_id, resource_type, resource_id)`` is a plain constraint
  (no nullable column participates), so no NULLS NOT DISTINCT / COALESCE
  dance is needed — unlike ``release_channels`` (PR-053).

What this revision does NOT do
------------------------------

* No write path — the import / promote paths do not yet project into
  ``catalog_entries`` (follow-up). ``GET /catalog`` returns ``[]`` until then.
* No skill / mcp / tool population — only the ``agent`` resource type has a
  data source today (the artifact tables); the others are future tracks.
* No Run pin — the ``runs`` table is unchanged (Run-pin is a separate PR
  per the PR-054 scope cut; this PR delivers the read-side resolver + the
  Catalog table only).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.migrations._helpers import safe_create_index, safe_create_table

# revision identifiers, used by Alembic.
revision: str = "0014_catalog_entries"
down_revision: str | Sequence[str] | None = "0013_release_channels_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``catalog_entries`` discovery-index table (data-model.md §6.6)."""
    safe_create_table(
        "catalog_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "resource_type IN ('agent', 'skill', 'mcp', 'tool')",
            name="ck_catalog_entries_resource_type",
        ),
        sa.CheckConstraint(
            "source IN ('database', 'file_import', 'system')",
            name="ck_catalog_entries_source",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'archived')",
            name="ck_catalog_entries_status",
        ),
        sa.UniqueConstraint(
            "org_id",
            "resource_type",
            "resource_id",
            name="uq_catalog_entries_org_resource",
        ),
    )
    safe_create_index("idx_catalog_entries_org", "catalog_entries", ["org_id"])
    safe_create_index("idx_catalog_entries_org_type", "catalog_entries", ["org_id", "resource_type"])
    safe_create_index("idx_catalog_entries_workspace", "catalog_entries", ["org_id", "workspace_id"])


def downgrade() -> None:
    """Drop the ``catalog_entries`` table (reverse index order)."""
    op.drop_index("idx_catalog_entries_workspace", table_name="catalog_entries")
    op.drop_index("idx_catalog_entries_org_type", table_name="catalog_entries")
    op.drop_index("idx_catalog_entries_org", table_name="catalog_entries")
    op.drop_table("catalog_entries")
