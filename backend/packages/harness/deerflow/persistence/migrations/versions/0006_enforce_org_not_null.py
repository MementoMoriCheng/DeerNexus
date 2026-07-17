"""Enforce ``org_id NOT NULL`` on the Run-lifecycle resource tables.

Revision ID: 0006_enforce_org_not_null
Revises: 0005_resource_org_id
Create Date: 2026-07-17

Track B (Schema Enforce) — PR-025A. Tightens the ``org_id`` column on the
four core Run-lifecycle stock tables (``threads_meta``, ``runs``,
``run_events``, ``feedback``) from nullable to ``NOT NULL``, and adds the
``UNIQUE(org_id, thread_id)`` compound-unique constraint on ``threads_meta``
declared by data-model.md §7.1. This is data-model.md §13.3 Enforce phase:
the schema layer now refuses NULL ``org_id`` writes, matching the application
guarantee PR-024 already enforced at the repository layer (every write stamps
``org_id`` from the bound tenant; missing tenant → fail-closed
``RuntimeError``).

Prerequisites (all satisfied as of this revision)
-------------------------------------------------

- Expand (PR-021, revision 0005): the ``org_id`` column exists on all four
  tables as ``nullable=True`` with a real RESTRICT FK ``fk_<table>_org_id``.
- Backfill (PR-023): every legacy row's ``org_id`` was populated to the
  default bootstrap Org, so the tables hold **zero NULL ``org_id``** today.
  The NOT NULL ALTER therefore loses no rows; a residual NULL would make the
  ALTER fail loud (the correct, surfacing failure mode — doctor (PR-025C) will
  later add a friendly pre-check).
- Repository Org Scope (PR-024): every new write stamps ``org_id`` from the
  bound tenant, so no new NULL is produced going forward.

Irreversibility (production semantics)
--------------------------------------

data-model.md §13.3 / §13.4 and progress.md classify Enforce as an
*irreversible* release: a production DB that has run this revision cannot
tolerate a NULL ``org_id`` write, and pr-split-guide.md §7 forbids collapsing
the Enforce sub-PRs (A/B/C/D) into a single release. This revision is the
schema half of Enforce; the multi-org Feature Flag (PR-025B), doctor enable
flow (PR-025C), and Contract cleanup (PR-025D) remain separate follow-ups.

The ``downgrade()`` re-relaxes the column to nullable and drops the compound
unique, which is safe in this single-Org window because PR-024 keeps stamping
``org_id`` on every write. It exists so a misconfigured staging env can
revert; production should treat the revision as one-way once observed stable
(production-runbook.md §8.2 / §8.3).

Idempotency
-----------

Uses the new ``safe_set_column_nullable`` helper (companion to
``safe_add_column`` / ``safe_create_index``) so the revision re-runs safely:
if the column is already NOT NULL (e.g. the legacy bootstrap branch's
``create_all`` provisioned it that way from current ORM metadata), the helper
no-ops after a drift check. ``safe_create_unique_constraint`` makes the
compound unique creation idempotent the same way. SQLite batch mode reflects
and re-applies the named ``fk_<table>_org_id`` RESTRICT FK during the rebuild,
so the FK is preserved across the nullability change.

The compound unique is created as a table-level ``UniqueConstraint`` (via
``batch_op.create_unique_constraint``), matching how the ORM declares it in
``ThreadMetaRow.__table_args__``. A unique *constraint* and a unique *index*
are physically identical on SQLite/Postgres but SQLAlchemy reflects them
through different inspectors, so the migration must create the artifact that
matches the ORM declaration to keep a fresh ``create_all`` DB and a migrated
DB structurally identical.

Scope of the compound unique
----------------------------

Only ``threads_meta`` gains ``UNIQUE(org_id, thread_id)`` here. The
``UNIQUE(org_id, idempotency_key)`` on ``runs`` (§7.2) depends on the
``idempotency_key`` column, which does not exist yet (ReleaseRef enforcement
track) and is therefore out of scope. The existing global uniques
``uq_events_thread_seq`` and ``uq_feedback_thread_run_user`` are left
unchanged; their org-scoping is a Contract-phase (PR-025D) cleanup that
coincides with the temporary compatible-index removal (§13.4).

Schema parity with ``Base.metadata``
------------------------------------

The ORM models (``thread_meta``, ``run``, ``run_event``, ``feedback``) are
updated in the same revision to declare ``org_id`` as ``nullable=False`` and
``threads_meta`` gains the matching ``UniqueConstraint``, so a fresh DB
provisioned by ``create_all`` + ``stamp head`` and a legacy-upgraded DB are
schema-identical (guarded by
``test_create_all_and_alembic_upgrade_produce_same_schema``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from deerflow.persistence.migrations._helpers import (
    safe_create_unique_constraint,
    safe_drop_unique_constraint,
    safe_set_column_nullable,
)

# Tables whose org_id is tightened to NOT NULL, matching 0005's set. All
# already carry the named RESTRICT FK fk_<table>_org_id from revision 0005.
_RESOURCE_TABLES: tuple[str, ...] = ("threads_meta", "runs", "run_events", "feedback")

# revision identifiers, used by Alembic.
revision: str = "0006_enforce_org_not_null"
down_revision: str | Sequence[str] | None = "0005_resource_org_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Tighten org_id to NOT NULL + add threads_meta UNIQUE(org_id, thread_id)."""
    for table in _RESOURCE_TABLES:
        safe_set_column_nullable(table, "org_id", nullable=False, existing_type=sa.String(length=36))

    # threads_meta — UNIQUE(org_id, thread_id) per data-model.md §7.1. thread_id
    # is already the global PK so the constraint is declaratively satisfied
    # today; it codifies the §1 #9 org-prefix-unique convention and primes the
    # schema for org-scoped business keys added in later tracks. Created as a
    # table-level UniqueConstraint to match the ORM declaration (see module
    # docstring "Idempotency").
    safe_create_unique_constraint(
        "uq_threads_meta_org_thread",
        "threads_meta",
        ["org_id", "thread_id"],
    )


def downgrade() -> None:
    """Re-relax org_id to nullable and drop the threads_meta compound unique."""
    safe_drop_unique_constraint("uq_threads_meta_org_thread", "threads_meta")

    for table in _RESOURCE_TABLES:
        safe_set_column_nullable(table, "org_id", nullable=True, existing_type=sa.String(length=36))
