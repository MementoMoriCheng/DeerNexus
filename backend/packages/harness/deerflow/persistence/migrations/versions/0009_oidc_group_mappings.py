"""Add the ``oidc_group_mappings`` allowlist table (PR-036).

Revision ID: 0009_oidc_group_mappings
Revises: 0008_service_account_fields
Create Date: 2026-07-23

Track C PR-036 materialises the ADR-0003 §10 OIDC group-mapping config
model as a runtime table so operators can manage the allowlist via the
IAM API (rule 5: "映射变更产生审计"). This revision is **expand-only /
additive**: it creates one new table. No existing table is modified and
no data is backfilled — the table is empty until this PR's write path
lands.

Schema parity with ``Base.metadata``
------------------------------------

The table mirrors ``deerflow.persistence.iam.model.OidcGroupMappingRow``
exactly so a fresh DB (provisioned by ``create_all`` + ``stamp head``)
and a legacy-upgraded DB are schema-identical. Uses ``safe_create_table``
/ ``safe_create_index`` (the idempotent helpers from PR-020A) so the full
table+index revision is re-runnable against a DB the legacy branch's
``create_all`` has already seeded.

Why a table (not config)
------------------------

ADR §10 explicitly requires "映射变更产生审计" — config-file changes can
only produce a config-reload log, but a DB row has a stable ``id`` the
audit payload can reference and every CRUD mutation is observable through
the ``emit_tenant_event`` shim (real AuditEvent outbox in PR-041). The
row set **is** the allowlist (§10 rule 1): an unmatched
``(issuer, group)`` is never mapped.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.migrations._helpers import safe_create_index, safe_create_table

# revision identifiers, used by Alembic.
revision: str = "0009_oidc_group_mappings"
down_revision: str | Sequence[str] | None = "0008_service_account_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``oidc_group_mappings`` allowlist table (ADR-0003 §10)."""
    safe_create_table(
        "oidc_group_mappings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("group_claim", sa.String(length=120), nullable=False),
        sa.Column("group_value", sa.String(length=200), nullable=False),
        sa.Column("target_org_id", sa.String(length=36), nullable=False),
        sa.Column("target_role_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "mode IN ('additive', 'authoritative')",
            name="ck_oidc_group_mappings_mode",
        ),
        sa.UniqueConstraint(
            "issuer",
            "group_value",
            "target_org_id",
            "target_role_id",
            name="uq_oidc_group_mappings_issuer_group_org_role",
        ),
    )
    safe_create_index(
        "idx_oidc_group_mappings_issuer",
        "oidc_group_mappings",
        ["issuer"],
    )
    safe_create_index(
        "idx_oidc_group_mappings_org",
        "oidc_group_mappings",
        ["target_org_id"],
    )


def downgrade() -> None:
    """Drop the ``oidc_group_mappings`` table (reverse FK / index order)."""
    op.drop_index("idx_oidc_group_mappings_org", table_name="oidc_group_mappings")
    op.drop_index("idx_oidc_group_mappings_issuer", table_name="oidc_group_mappings")
    op.drop_table("oidc_group_mappings")
