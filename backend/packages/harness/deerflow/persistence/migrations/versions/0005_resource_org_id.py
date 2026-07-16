"""Add nullable ``org_id`` to stock Run-lifecycle resource tables.

Revision ID: 0005_resource_org_id
Revises: 0004_iam_tables
Create Date: 2026-07-16

Track B (Schema Expand) — PR-021. Adds a nullable ``org_id`` column plus
org_id-prefixed *compatible* indexes to the four core Run-lifecycle stock
tables (``threads_meta``, ``runs``, ``run_events``, ``feedback``). This is
the first step of the data-model.md §13.1 Expand phase: stock resources
gain an optional ``org_id`` column so new writes can carry a tenant while
legacy rows stay NULL until the backfill job (PR-023) populates them.

Scope
-----

Only the four tables that exist in ``0001_baseline`` and carry tenant-scoped
business rows receive ``org_id`` here. The control-plane tables (PR-020A/B),
the channel_* tables (pending a ``channel_bindings`` model decision,
data-model.md §10.1; ``channel_connections.workspace_id`` already names the
external IM workspace and would collide), the users identity table (covered
by ``org_memberships`` / ``external_identities``), and the LangGraph-owned
checkpoint tables (excluded from baseline ownership by ``env.py``) are all
out of scope. Memory/Artifact/Skill/MCP/Scheduler tables do not exist yet
and are introduced by their own later-Track schema PRs.

Column contract
---------------

``org_id`` is ``String(36)`` (matching ``organizations.id``, which is a
hex/uuid string — not a native ``uuid`` type), ``nullable=True`` (Expand),
no ``server_default`` (pr-split-guide §14 forbids using ORM defaults to
mask stock NULLs). It is a **real FK** to ``organizations.id`` with
``ondelete=RESTRICT``: a resource must not be silently lost when an org is
hard-deleted (orgs normally soft-delete via ``deleted_at``). NOT NULL and
compound-unique enforcement are Enforce actions (data-model.md §13.3),
deferred to PR-025A. No backfill is performed here (PR-023).

Schema parity with ``Base.metadata``
------------------------------------

The ORM models (``thread_meta``, ``run``, ``run_event``, ``feedback``)
declare ``org_id`` with the same type / nullable / FK, and the five
compatible indexes, so a fresh DB provisioned by ``create_all`` +
``stamp head`` and a legacy-upgraded DB are schema-identical (guarded by
``test_create_all_and_alembic_upgrade_produce_same_schema``).

Idempotency
-----------

Uses ``safe_add_column`` (the column helper from revision 0002) and
``safe_create_index`` (the index helper from PR-020A) so the revision is
re-runnable. This covers the legacy bootstrap branch, where the restricted
``create_all`` seeds the baseline tables (now including the new org_id
column and indexes, since ``create_all`` builds from current ORM metadata)
before ``upgrade head`` runs — at which point the helpers no-op on the
already-present column/index.

Compatible indexes
------------------

The five indexes are org_id-prefixed per data-model.md §1 #9 and mirror
each table's primary query path. They are *temporary* (§13.4): the Contract
phase cleans them up alongside the dual-write columns. Existing single-column
indexes are intentionally left in place for the dual-read compatibility
window.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from deerflow.persistence.migrations._helpers import (
    safe_add_column,
    safe_create_index,
    safe_drop_column,
    safe_drop_index,
)

# Tables that gain org_id, in FK-definition order (all reference
# organizations.id, which already exists after 0003_tenant_tables).
_RESOURCE_TABLES: tuple[str, ...] = ("threads_meta", "runs", "run_events", "feedback")

# revision identifiers, used by Alembic.
revision: str = "0005_resource_org_id"
down_revision: str | Sequence[str] | None = "0004_iam_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _org_id_column(table: str) -> sa.Column:
    # The FK constraint is named explicitly: alembic's SQLite batch mode
    # (used by ``safe_add_column``) reflects and re-applies constraints when
    # rebuilding the table during later ALTERs, and requires every constraint
    # to carry a name. The ORM model's inline FK stays anonymous (matching
    # ``workspaces.org_id`` / ``roles.org_id``), which is fine for the
    # ``create_all`` fresh-DB path; only the migration-time batch ALTER needs
    # the name. The naming convention is ``fk_<table>_org_id``.
    return sa.Column(
        "org_id",
        sa.String(length=36),
        sa.ForeignKey("organizations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id"),
        nullable=True,
    )


def upgrade() -> None:
    """Add nullable ``org_id`` + org_id-prefixed compatible indexes (§7.1/§7.2)."""
    for table in _RESOURCE_TABLES:
        safe_add_column(table, _org_id_column(table))

    # threads_meta — org-scoped recent-thread listing (§7.1; workspace_id deferred).
    safe_create_index("ix_threads_meta_org_updated", "threads_meta", ["org_id", "updated_at"])
    # runs — org-scoped run status listing and per-thread run history (§7.2).
    safe_create_index("ix_runs_org_status_created", "runs", ["org_id", "status", "created_at"])
    safe_create_index("ix_runs_org_thread_created", "runs", ["org_id", "thread_id", "created_at"])
    # run_events — org-scoped event lookup (mirrors ix_events_run, org_id-prefixed).
    safe_create_index("ix_events_org_thread_run", "run_events", ["org_id", "thread_id", "run_id"])
    # feedback — org-scoped feedback lookup (mirrors feedback thread index, org_id-prefixed).
    safe_create_index("ix_feedback_org_thread", "feedback", ["org_id", "thread_id"])


def downgrade() -> None:
    """Drop ``org_id`` from the four stock tables.

    The compatible indexes are dropped *first* because on SQLite
    ``safe_drop_column`` rebuilds the table via ``batch_alter_table`` and
    re-creates every reflected index on the rebuilt table — an index that
    still names ``org_id`` would then fail with ``no such column``. Dropping
    the org_id-prefixed indexes first lets the batch rebuild recreate the
    surviving indexes cleanly.
    """
    safe_drop_index("ix_feedback_org_thread", "feedback")
    safe_drop_index("ix_events_org_thread_run", "run_events")
    safe_drop_index("ix_runs_org_thread_created", "runs")
    safe_drop_index("ix_runs_org_status_created", "runs")
    safe_drop_index("ix_threads_meta_org_updated", "threads_meta")

    for table in _RESOURCE_TABLES:
        safe_drop_column(table, "org_id")
