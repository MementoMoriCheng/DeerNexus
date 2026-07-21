"""Add traceability columns to ``service_accounts`` (PR-034).

Revision ID: 0008_service_account_fields
Revises: 0007_builtin_roles
Create Date: 2026-07-21

Track C PR-034 fills in the ADR-0003 §9.1 traceability fields on the
``service_accounts`` table introduced by ``0004_iam_tables``. All five
new columns are **nullable** — expand-only — so existing rows (none
exist at PR-034 entry: the table is empty until this PR's write path
lands) and any future backfilled row survive unchanged.

The new columns:

* ``owner_user_id`` — ADR §9.1 "Owner 是管理责任人,不意味着自动拥有该
  账号权限". Accountability contact only; never a grant source. Stored
  as ``String(36)`` UUID without an FK, mirroring the polymorphic
  principal convention used throughout the IAM tables
  (``role_bindings.principal_id`` likewise has no FK).
* ``purpose`` / ``system`` / ``environment`` — free-text traceability
  fields ("用途、系统、环境... 必须可追踪"). Bounded strings so a
  misbehaving client cannot bloat the row.
* ``expires_at`` — review-by date ("到期评审日期必须可追踪"), NOT a
  credential expiry. The ServiceAccount itself does not expire
  automatically; this field is an operator-negotiated review
  checkpoint.

No new constraints or indexes: the existing
``ck_service_accounts_status`` and ``uq_service_accounts_org_name``
from ``0004_iam_tables`` remain authoritative.

Idempotence
-----------

Uses :func:`safe_add_column` so the revision is safe to re-run on a DB
that already has any of the columns (legacy branch whose
``create_all`` provisions the model with all fields present).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from deerflow.persistence.migrations._helpers import safe_add_column, safe_drop_column

# revision identifiers, used by Alembic.
revision: str = "0008_service_account_fields"
down_revision: str | Sequence[str] | None = "0007_builtin_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the five ADR §9.1 traceability columns to ``service_accounts``."""
    safe_add_column("service_accounts", sa.Column("owner_user_id", sa.String(length=36), nullable=True))
    safe_add_column("service_accounts", sa.Column("purpose", sa.String(length=256), nullable=True))
    safe_add_column("service_accounts", sa.Column("system", sa.String(length=64), nullable=True))
    safe_add_column("service_accounts", sa.Column("environment", sa.String(length=32), nullable=True))
    safe_add_column("service_accounts", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Reverse the column additions in reverse dependency order.

    All five columns are independent (no FK / index depends on any other
    one), so the drop order is purely convention: reverse the upgrade
    order. Each helper no-ops if the column is already gone.
    """
    safe_drop_column("service_accounts", "expires_at")
    safe_drop_column("service_accounts", "environment")
    safe_drop_column("service_accounts", "system")
    safe_drop_column("service_accounts", "purpose")
    safe_drop_column("service_accounts", "owner_user_id")
