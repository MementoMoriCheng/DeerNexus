"""Add tenant control-plane tables (organizations / workspaces / external_identities / org_memberships).

Revision ID: 0003_tenant_tables
Revises: 0002_runs_token_usage
Create Date: 2026-07-16

Track B (Schema Expand) entry — first half of PR-020, split per
``pr-split-guide.md`` §7 (PR-020A = tenant tables, PR-020B = IAM tables).
This revision is **expand-only / additive**: it creates four new
control-plane tables and references the existing ``users`` table (created by
``0001_baseline``) as an FK target without modifying it. No existing
DeerFlow resource table gains an ``org_id`` column here — that is PR-021.

Schema parity with ``Base.metadata``
------------------------------------

The four tables mirror ``deerflow.persistence.orgs.model`` exactly so a
fresh DB (provisioned by ``create_all`` + ``stamp head``) and a
legacy-upgraded DB are schema-identical. Column types are cross-dialect
(``JSON`` not ``JSONB``, ``DateTime(timezone=True)`` not ``TIMESTAMPTZ``)
because the test suite runs on aiosqlite; ``env.py``'s
``_type_equivalent`` treats ``JSON`` and ``JSONB`` as equivalent so
Postgres deployments stay quiet.

Idempotency
-----------

Uses ``safe_create_table`` (the table-level analogue of ``safe_add_column``)
so re-running this revision against a DB that already has a table is a
no-op. The bootstrap three-branch decision already keeps ``create_table``
safe, but ``safe_create_table`` covers manual ``CREATE TABLE`` workarounds,
partially-applied re-runs, and any race that bypasses the bootstrap lock.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.migrations._helpers import safe_create_index, safe_create_table

# revision identifiers, used by Alembic.
revision: str = "0003_tenant_tables"
down_revision: str | Sequence[str] | None = "0002_runs_token_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the tenant control-plane tables (data-model.md §4.1, §4.2, §4.4, §4.5)."""
    # organizations (§4.1) — created first; workspaces & org_memberships FK it.
    safe_create_table(
        "organizations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'deleting', 'deleted')",
            name="ck_organizations_status",
        ),
    )
    safe_create_index(
        "uq_organizations_slug_active",
        "organizations",
        ["slug"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # workspaces (§4.2)
    safe_create_table(
        "workspaces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_workspaces_status"),
        sa.UniqueConstraint("org_id", "slug", name="uq_workspaces_org_slug"),
    )
    safe_create_index(
        "idx_workspaces_org_status",
        "workspaces",
        ["org_id", "status"],
    )

    # external_identities (§4.4) — FK users (existing baseline table).
    safe_create_table(
        "external_identities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("claims_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_external_identities_issuer_subject"),
    )
    safe_create_index(
        "idx_external_identities_user",
        "external_identities",
        ["user_id"],
    )

    # org_memberships (§4.5) — FK organizations + users.
    safe_create_table(
        "org_memberships",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'removed')",
            name="ck_org_memberships_status",
        ),
        sa.UniqueConstraint("org_id", "user_id", name="uq_org_memberships_org_user"),
    )
    safe_create_index(
        "idx_org_memberships_user_status",
        "org_memberships",
        ["user_id", "status"],
    )


def downgrade() -> None:
    """Drop the tenant control-plane tables (reverse FK dependency order)."""
    op.drop_index("idx_org_memberships_user_status", table_name="org_memberships")
    op.drop_table("org_memberships")

    op.drop_index("idx_external_identities_user", table_name="external_identities")
    op.drop_table("external_identities")

    op.drop_index("idx_workspaces_org_status", table_name="workspaces")
    op.drop_table("workspaces")

    op.drop_index("uq_organizations_slug_active", table_name="organizations")
    op.drop_table("organizations")
