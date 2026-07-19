"""Add template_version column and seed builtin Org roles (PR-030).

Revision ID: 0007_builtin_roles
Revises: 0006_enforce_org_not_null
Create Date: 2026-07-19

Track C entry point (pr-split-guide.md §8, PR-030). Two additive changes to
the existing ``roles`` table introduced by ``0004_iam_tables``:

1. Add ``template_version`` (BigInteger, nullable). Only builtin system
   templates carry a non-null value; custom roles stay NULL. Bumped on every
   seed migration that changes any builtin role's permission set, so audits
   and future upgrades can correlate a row with the seed revision that
   produced it (ADR-0003 §5: "内置角色变更必须有迁移、变更记录和回归测试").

2. Seed the three builtin Org roles (``org:admin`` / ``org:developer`` /
   ``org:viewer``) as system templates (``org_id IS NULL`` +
   ``is_system = true``) with the permission sets frozen in
   ``deerflow.contracts.rbac.BUILTIN_ROLE_PERMISSIONS``. The same registry is
   consumed by ``tenancy/bootstrap.py::ensure_builtin_roles``, so the fresh-DB
   (``create_all`` + ``stamp head``) and legacy/versioned-upgrade paths
   converge on byte-identical role content.

What this revision deliberately does NOT do (Track C boundary, PR-031+):

* No runtime authorization logic. ``roles.permissions`` is data; the
  effective-permission intersection formula (ADR-0003 §6) is PR-031.
* No router changes. Existing ``@require_permission`` decorators stay on the
  flat-permission stub until PR-031/033 swap them out.
* No ``system:admin`` seed. ADR-0003 §4.4 keeps it independent of ordinary
  RoleBinding; seeding it as a role row would imply the wrong grant path.

Idempotence
-----------

The bootstrap's empty branch (``create_all`` + ``stamp head``) never runs
this revision — the lifespan helper ``ensure_builtin_roles`` provisions the
same rows from the same registry. The legacy and versioned branches re-run
revisions, so ``upgrade`` must be safe to apply on a DB where the lifespan
helper has already inserted rows (legacy DB upgraded after a boot). The seed
therefore probes by ``(name, is_system)`` and UPDATEs existing rows to the
current permission set rather than INSERT-or-duplicate-error. ``system:admin``
(never seeded here) and tenant-defined roles (none exist yet at PR-030) are
both outside the ``name IN (...) AND is_system`` predicate and untouched.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from deerflow.contracts.rbac import (
    BUILTIN_ROLE_PERMISSIONS,
    BUILTIN_ROLE_TEMPLATE_VERSION,
    ORG_ADMIN_ROLE_NAME,
    ORG_DEVELOPER_ROLE_NAME,
    ORG_VIEWER_ROLE_NAME,
)
from deerflow.persistence.migrations._helpers import safe_add_column, safe_drop_column

# revision identifiers, used by Alembic.
revision: str = "0007_builtin_roles"
down_revision: str | Sequence[str] | None = "0006_enforce_org_not_null"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Names seeded by this revision. Kept as a module-level tuple so downgrade()
# can target exactly these rows and never a tenant-defined role of the same
# name (tenant roles have is_system = false and fall outside the predicate).
_SEED_ROLE_NAMES: tuple[str, ...] = (
    ORG_ADMIN_ROLE_NAME,
    ORG_DEVELOPER_ROLE_NAME,
    ORG_VIEWER_ROLE_NAME,
)


def upgrade() -> None:
    """Add ``template_version`` and seed the three builtin Org roles."""
    # 1. Additive column (expand-only). Nullable so custom roles stay NULL.
    safe_add_column("roles", sa.Column("template_version", sa.BigInteger(), nullable=True))

    # 2. Seed builtin roles. Build a lightweight table object bound to the
    # current connection so we can SELECT / INSERT / UPDATE without a full ORM
    # session (alembic migrations run synchronously through op.get_bind()).
    bind = op.get_bind()
    roles = sa.table(
        "roles",
        sa.Column("id", sa.String(length=36)),
        sa.Column("org_id", sa.String(length=36)),
        sa.Column("name", sa.String(length=100)),
        sa.Column("description", sa.Text()),
        sa.Column("permissions", sa.JSON()),
        sa.Column("is_system", sa.Boolean()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("row_version", sa.BigInteger()),
        sa.Column("template_version", sa.BigInteger()),
    )

    for name in _SEED_ROLE_NAMES:
        permissions = sorted(p.value for p in BUILTIN_ROLE_PERMISSIONS[name])
        existing = bind.execute(
            sa.select(roles.c.id).where(
                roles.c.name == name,
                roles.c.is_system.is_(True),
            )
        ).scalar_one_or_none()

        if existing is not None:
            # Legacy/versioned path: lifespan helper (or a prior upgrade) has
            # already inserted the row. Re-align its permissions + template
            # version to the current registry so all three bootstrap paths
            # converge on identical content.
            bind.execute(
                roles.update()
                .where(roles.c.id == existing)
                .values(
                    permissions=permissions,
                    template_version=BUILTIN_ROLE_TEMPLATE_VERSION,
                )
            )
        else:
            now = datetime.now(UTC)
            bind.execute(
                roles.insert().values(
                    id=uuid.uuid4().hex,
                    org_id=None,
                    name=name,
                    description=f"Builtin Org role seeded by PR-030 (template v{BUILTIN_ROLE_TEMPLATE_VERSION}).",
                    permissions=permissions,
                    is_system=True,
                    created_at=now,
                    updated_at=now,
                    row_version=1,
                    template_version=BUILTIN_ROLE_TEMPLATE_VERSION,
                )
            )


def downgrade() -> None:
    """Reverse the seed and drop ``template_version``.

    The DELETE targets only system templates with the three seeded names, so
    tenant-defined roles (which cannot exist yet at PR-030, but may in the
    future) are never swept up. Dropping the column is the additive inverse of
    ``upgrade``'s ``safe_add_column`` and is safe against legacy DBs that
    never had it.
    """
    bind = op.get_bind()
    roles = sa.table("roles", sa.Column("name", sa.String(length=100)), sa.Column("is_system", sa.Boolean()))
    bind.execute(
        sa.delete(roles).where(
            roles.c.name.in_(_SEED_ROLE_NAMES),
            roles.c.is_system.is_(True),
        )
    )
    safe_drop_column("roles", "template_version")
