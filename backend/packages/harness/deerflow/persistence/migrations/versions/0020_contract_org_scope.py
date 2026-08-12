"""Contract phase: drop temporary compatible indexes and org-scope global uniques.

Revision ID: 0020_contract_org_scope
Revises: 0019_model_providers
Create Date: 2026-08-12

Track B (Schema Contract) — PR-025D. The final step of the Expand → Backfill →
Enforce → Contract pipeline (data-model.md §13.4). Two things happen here:

1. **Drop the five temporary compatible indexes** created by revision 0005
   (``ix_*_org_*``). These were org_id-prefixed shadows of the pre-existing
   single-column indexes, kept for the dual-read compatibility window. Now
   that ``org_id`` is the sole tenant boundary at the storage layer
   (application-layer ``user_id`` WHERE branches removed in this same PR),
   every query already filters on ``org_id``; the old single-column indexes
   that survived 0005 remain sufficient.

2. **Org-scope the two remaining global unique constraints** that 0006 left
   for Contract:

   - ``uq_events_thread_seq(thread_id, seq)`` → ``uq_events_org_thread_seq(org_id, thread_id, seq)``
   - ``uq_feedback_thread_run_user(thread_id, run_id, user_id)`` → ``uq_feedback_org_thread_run_user(org_id, thread_id, run_id, user_id)``

   ``threads_meta`` already gained ``UNIQUE(org_id, thread_id)`` in revision
   0006 (PR-025A). ``runs.UNIQUE(org_id, idempotency_key)`` depends on a
   column that does not exist yet (ReleaseRef enforcement track) and is out
   of scope.

The constraint *rename* (not just re-column) is deliberate: the old names
are referenced by the downgrade path and by tests that pin the pre-Contract
schema. A fresh ``create_all`` DB built from current ORM metadata must
reflect the new names so the parity guard
(``test_create_all_and_alembic_upgrade_produce_same_schema``) stays green.

Irreversibility (production semantics)
--------------------------------------

Per ci-cd §10.2, Contract must land at least one stable window after Enforce
and after the multi-org Feature Flag is flipped to ``active`` in production.
Once Contract has shipped, pre-Contract application versions (that still
filter on ``user_id`` and expect the old constraint names) can no longer
connect safely — the old global uniques no longer exist and the compatible
indexes are gone. The ``downgrade()`` exists for staging rollback but should
not be relied upon in production (production-runbook §8.2 / §8.3).

Idempotency
-----------

Uses ``safe_drop_index`` / ``safe_drop_unique_constraint`` /
``safe_create_unique_constraint`` so the revision re-runs cleanly on a DB
that is already at the Contract state (e.g. the legacy bootstrap branch
where ``create_all`` provisions the post-Contract schema before ``upgrade
head`` runs).
"""

from __future__ import annotations

from collections.abc import Sequence

from deerflow.persistence.migrations._helpers import (
    safe_create_index,
    safe_create_unique_constraint,
    safe_drop_index,
    safe_drop_unique_constraint,
)

# The five temporary org_id-prefixed indexes created in revision 0005.
_COMPATIBLE_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_threads_meta_org_updated", "threads_meta", ["org_id", "updated_at"]),
    ("ix_runs_org_status_created", "runs", ["org_id", "status", "created_at"]),
    ("ix_runs_org_thread_created", "runs", ["org_id", "thread_id", "created_at"]),
    ("ix_events_org_thread_run", "run_events", ["org_id", "thread_id", "run_id"]),
    ("ix_feedback_org_thread", "feedback", ["org_id", "thread_id"]),
)

# revision identifiers, used by Alembic.
revision: str = "0020_contract_org_scope"
down_revision: str | Sequence[str] | None = "0019_model_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop compatible indexes + org-scope the two global unique constraints."""
    # 1. Drop the five temporary compatible indexes (§13.4).
    for index_name, table_name, _cols in _COMPATIBLE_INDEXES:
        safe_drop_index(index_name, table_name)

    # 2. Org-scope run_events unique: (thread_id, seq) → (org_id, thread_id, seq).
    safe_drop_unique_constraint("uq_events_thread_seq", "run_events")
    safe_create_unique_constraint("uq_events_org_thread_seq", "run_events", ["org_id", "thread_id", "seq"])

    # 3. Org-scope feedback unique: (thread_id, run_id, user_id)
    #    → (org_id, thread_id, run_id, user_id).
    safe_drop_unique_constraint("uq_feedback_thread_run_user", "feedback")
    safe_create_unique_constraint(
        "uq_feedback_org_thread_run_user",
        "feedback",
        ["org_id", "thread_id", "run_id", "user_id"],
    )


def downgrade() -> None:
    """Restore the pre-Contract global uniques and compatible indexes.

    Reverses the upgrade so a staging env can revert. Production should treat
    the Contract revision as one-way once observed stable (production-runbook
    §8.2 / §8.3).
    """
    # Restore feedback global unique.
    safe_drop_unique_constraint("uq_feedback_org_thread_run_user", "feedback")
    safe_create_unique_constraint("uq_feedback_thread_run_user", "feedback", ["thread_id", "run_id", "user_id"])

    # Restore run_events global unique.
    safe_drop_unique_constraint("uq_events_org_thread_seq", "run_events")
    safe_create_unique_constraint("uq_events_thread_seq", "run_events", ["thread_id", "seq"])

    # Recreate the five temporary compatible indexes.
    for index_name, table_name, cols in _COMPATIBLE_INDEXES:
        safe_create_index(index_name, table_name, cols)
