"""Pin a ``ReleaseRef`` onto ``runs`` and mark legacy runs (PR-056).

Revision ID: 0016_run_release_pin
Revises: 0015_release_idempotency
Create Date: 2026-07-27

Track E PR-056 lands ADR-0004 §6 step 7 (persist the resolved ReleaseRef into
``runs``) and §12 legacy Run gating. It absorbs the "Run-pin write side"
that PR-052/053/054/055 each explicitly deferred to a follow-up PR: there is no
separate PR slot after PR-055, and the §12 gate cannot fire without a column
to read, so PR-056 carries both the write side and the read side together.

Five additive, **nullable** columns on ``runs``:

* ``release_package_id`` / ``release_version_id`` — frozen identity of the
  package + version the run was pinned to at creation (ADR §6 step 9: the
  execution phase only consumes the persisted ReleaseRef, it does not
  re-read the channel, so the run never drifts on a later promote/rollback).
* ``release_channel`` — ``dev`` / ``staging`` / ``prod``.
* ``release_digest`` — ``sha256:<hex>`` artifact digest, the execution
  identity the run was admitted against.
* ``legacy_unpinned`` — boolean flag marking runs created before release
  enforcement (the "存量/legacy" runs of ADR §12). Defaults **true** so every
  pre-existing row becomes legacy at ALTER time; new runs written while
  ``production.agent_release.enforce`` is off are also legacy (the resolver
  is not called, no pin is written).

**No FK** on the release columns. A pinned run is a frozen snapshot — the
digest is the integrity guarantee, not a relational join. This mirrors the
``release_idempotency_records`` decision (PR-055 revision 0015): replay/pin
records are self-contained so GC of a release resource never silently orphans
a run. The same reasoning applies to ``release_version_id``: a revoked/archived
version must not cascade-delete the run that already executed against it.

What this revision deliberately does NOT do
------------------------------------------

* No ``policy_version`` column — there is no policy computation path today
  (Track E). Adding it later is additive.
* No Run-level ``idempotency_key`` — Run-creation dedup is a distinct
  feature from promote/rollback Idempotency-Key replay (PR-055) and is left
  to its own follow-up.
* No backfill that re-resolves existing runs against a channel — ADR §12
  forbids guessing a historical digest "from the current disk version";
  legacy rows stay ``legacy_unpinned = true`` and are read-only / cancel /
  archive only. A future one-shot migration may pin a run when its exact
  historical version + digest can be proven.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from deerflow.persistence.migrations._helpers import safe_add_column, safe_drop_column

# revision identifiers, used by Alembic.
revision: str = "0016_run_release_pin"
down_revision: str | Sequence[str] | None = "0015_release_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Ordered so downgrade() drops in reverse.
_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    ("release_package_id", sa.Column("release_package_id", sa.String(length=36), nullable=True)),
    ("release_version_id", sa.Column("release_version_id", sa.String(length=36), nullable=True)),
    ("release_channel", sa.Column("release_channel", sa.String(length=16), nullable=True)),
    ("release_digest", sa.Column("release_digest", sa.String(length=96), nullable=True)),
    # NOT NULL with server_default "true": the ALTER stamps every existing row
    # as legacy at upgrade time (ADR §12), and every new row written while
    # ``production.agent_release.enforce`` is off also inherits the default,
    # keeping it legacy until start_run pins it (PR-056 services.py). NOT NULL
    # (not nullable) mirrors the ORM ``Mapped[bool]`` with ``default=True``,
    # which SQLAlchemy renders as NOT NULL — parity between create_all and the
    # migration (test_create_all_and_alembic_upgrade_produce_same_schema).
    ("legacy_unpinned", sa.Column("legacy_unpinned", sa.Boolean(), nullable=False, server_default=sa.text("true"))),
)


def upgrade() -> None:
    """Add the five release-pin columns to ``runs``."""
    for _name, column in _COLUMNS:
        safe_add_column("runs", column)


def downgrade() -> None:
    """Drop the release-pin columns in reverse order."""
    for name, _column in reversed(_COLUMNS):
        safe_drop_column("runs", name)
