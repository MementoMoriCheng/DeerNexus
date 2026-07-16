"""Add IAM control-plane tables (roles / role_bindings / service_accounts / api_keys).

Revision ID: 0004_iam_tables
Revises: 0003_tenant_tables
Create Date: 2026-07-16

Track B (Schema Expand) — second half of PR-020, split per
``pr-split-guide.md`` §7 (PR-020A = tenant tables, PR-020B = IAM tables).
This revision is **expand-only / additive**: it creates four new IAM tables.
``role_bindings.role_id`` and ``api_keys.service_account_id`` FK into the
new tables; ``role_bindings`` references ``users`` / ``service_accounts``
polymorphically (no FK, per §5.2). No existing table is modified.

Schema parity with ``Base.metadata``
------------------------------------

The four tables mirror ``deerflow.persistence.iam.model`` exactly so a
fresh DB (provisioned by ``create_all`` + ``stamp head``) and a
legacy-upgraded DB are schema-identical. Uses ``safe_create_table`` /
``safe_create_index`` (the idempotent helpers from PR-020A) so the full
table+index revision is re-runnable against a DB the legacy branch's
``create_all`` has already seeded.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.migrations._helpers import safe_create_index, safe_create_table

# revision identifiers, used by Alembic.
revision: str = "0004_iam_tables"
down_revision: str | Sequence[str] | None = "0003_tenant_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the IAM control-plane tables (data-model.md §5.1, §5.2, §4.6, §4.7)."""
    # roles (§5.1) — created first; role_bindings FKs it.
    safe_create_table(
        "roles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # org_id IS NULL only allowed when is_system = true (ADR-0002 §4.1).
        sa.CheckConstraint(
            "(org_id IS NOT NULL) OR (is_system = 1)",
            name="ck_roles_system_template_allows_null_org",
        ),
    )
    safe_create_index(
        "uq_roles_org_name",
        "roles",
        ["org_id", "name"],
        unique=True,
        sqlite_where=sa.text("org_id IS NOT NULL"),
        postgresql_where=sa.text("org_id IS NOT NULL"),
    )

    # role_bindings (§5.2) — FK roles; polymorphic principal (no FK to
    # users/service_accounts — integrity via write-service + triggers).
    safe_create_table(
        "role_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("role_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "principal_type IN ('user', 'service_account')",
            name="ck_role_bindings_principal_type",
        ),
        sa.UniqueConstraint(
            "org_id",
            "principal_type",
            "principal_id",
            "role_id",
            name="uq_role_bindings_org_principal_role",
        ),
    )
    safe_create_index(
        "idx_role_bindings_principal",
        "role_bindings",
        ["principal_type", "principal_id"],
    )
    safe_create_index(
        "idx_role_bindings_org",
        "role_bindings",
        ["org_id"],
    )

    # service_accounts (§4.6)
    safe_create_table(
        "service_accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_service_accounts_status",
        ),
        sa.UniqueConstraint("org_id", "name", name="uq_service_accounts_org_name"),
    )
    safe_create_index(
        "idx_service_accounts_org",
        "service_accounts",
        ["org_id"],
    )

    # api_keys (§4.7) — FK service_accounts; hash-only (no recoverable key).
    safe_create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("service_account_id", sa.String(length=36), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=255), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["service_account_id"],
            ["service_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_prefix", name="uq_api_keys_key_prefix"),
    )
    safe_create_index(
        "idx_api_keys_org_sa",
        "api_keys",
        ["org_id", "service_account_id"],
    )


def downgrade() -> None:
    """Drop the IAM control-plane tables (reverse FK dependency order)."""
    op.drop_index("idx_api_keys_org_sa", table_name="api_keys")
    op.drop_table("api_keys")

    op.drop_index("idx_service_accounts_org", table_name="service_accounts")
    op.drop_table("service_accounts")

    op.drop_index("idx_role_bindings_org", table_name="role_bindings")
    op.drop_index("idx_role_bindings_principal", table_name="role_bindings")
    op.drop_table("role_bindings")

    op.drop_index("uq_roles_org_name", table_name="roles")
    op.drop_table("roles")
