"""Add the ``agent_packages`` / ``agent_versions`` tables (PR-050).

Revision ID: 0012_agent_artifacts
Revises: 0011_audit_outbox
Create Date: 2026-07-25

Track E PR-050 materialises the ADR-0004 §3 agent-artifact model as two
tables — ``agent_packages`` (stable logical identity, data-model.md §6.2)
and ``agent_versions`` (immutable content version, §6.3). These are the
authority rows ``prod`` Runs resolve against once the ReleaseRef path
(PR-053/054) is wired; until then this PR lands the tables with **no
callers**, following the "land the table first" philosophy established by
PR-030 (builtin-roles table) and PR-040 (audit_events table).

This revision is **expand-only / additive**: it creates two new tables and
their indexes. No existing table is modified and no data is backfilled —
both tables are empty until PR-052's write path lands.

Schema parity with ``Base.metadata``
------------------------------------

The tables mirror ``deerflow.persistence.release.model.AgentPackageRow``
and ``AgentVersionRow`` exactly so a fresh DB (provisioned by
``create_all`` + ``stamp head``) and a legacy-upgraded DB are
schema-identical. Uses ``safe_create_table`` / ``safe_create_index`` (the
idempotent helpers from PR-020A) so the full table+index revision is
re-runnable against a DB the legacy branch's ``create_all`` has already
seeded.

``agent_versions.package_id`` carries ``ON DELETE RESTRICT`` (ADR §3.1:
"a Package with existing Versions cannot be hard-deleted"). The
``content_inline`` / ``object_key`` XOR is enforced by a CHECK using the
explicit ``IS [NOT] NULL`` form so it evaluates identically on SQLite and
Postgres (avoiding the three-valued-logic pitfall of ``col1 <> col2`` with
NULLs).

What this revision does NOT do
------------------------------

* No ``published``-immutability trigger — the freeze of content / manifest /
  digest / version once ``status='published'`` is a write-path concern
  enforced by the repository (PR-052), not a DB trigger (unlike
  audit_events' append-only guard, this is a *conditional* on status, which
  has no cross-dialect trigger precedent here).
* No ``release_channels`` / ``release_events`` — those land in PR-053.
* No SemVer CHECK on ``version`` — ``digest`` is the execution identity;
  ``version`` is display-only (mirrors ``contracts/release.py``), SemVer
  enforcement is a write-path concern (PR-052).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.migrations._helpers import safe_create_index, safe_create_table

# revision identifiers, used by Alembic.
revision: str = "0012_agent_artifacts"
down_revision: str | Sequence[str] | None = "0011_audit_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``agent_packages`` and ``agent_versions`` tables (ADR-0004 §3)."""
    # agent_packages first — agent_versions.package_id references it.
    safe_create_table(
        "agent_packages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_agent_packages_status",
        ),
        sa.UniqueConstraint("org_id", "name", name="uq_agent_packages_org_name"),
    )
    safe_create_index("idx_agent_packages_org", "agent_packages", ["org_id"])

    # agent_versions — depends on agent_packages via RESTRICT FK.
    safe_create_table(
        "agent_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("content_inline", sa.Text(), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["agent_packages.id"],
            ondelete="RESTRICT",
            name="fk_agent_versions_package_id",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'reviewed', 'published', 'revoked', 'archived')",
            name="ck_agent_versions_status",
        ),
        sa.CheckConstraint(
            "(content_inline IS NOT NULL AND object_key IS NULL) OR (content_inline IS NULL AND object_key IS NOT NULL)",
            name="ck_agent_versions_content_exclusive",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="ck_agent_versions_size_nonneg",
        ),
        sa.UniqueConstraint(
            "org_id",
            "package_id",
            "version",
            name="uq_agent_versions_pkg_version",
        ),
        sa.UniqueConstraint("org_id", "digest", name="uq_agent_versions_org_digest"),
    )
    safe_create_index("idx_agent_versions_org", "agent_versions", ["org_id"])
    safe_create_index("idx_agent_versions_package", "agent_versions", ["package_id"])
    safe_create_index("idx_agent_versions_org_status", "agent_versions", ["org_id", "status"])


def downgrade() -> None:
    """Drop the two tables (reverse FK / index order: versions before packages)."""
    op.drop_index("idx_agent_versions_org_status", table_name="agent_versions")
    op.drop_index("idx_agent_versions_package", table_name="agent_versions")
    op.drop_index("idx_agent_versions_org", table_name="agent_versions")
    op.drop_table("agent_versions")
    op.drop_index("idx_agent_packages_org", table_name="agent_packages")
    op.drop_table("agent_packages")
