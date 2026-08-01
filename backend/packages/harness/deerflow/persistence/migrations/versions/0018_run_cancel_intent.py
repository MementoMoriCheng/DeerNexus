"""Add persisted cancel-intent columns to ``runs`` (PR-077 / ADR-0006 §5.4).

Revision ID: 0018_run_cancel_intent
Revises: 0017_run_row_version
Create Date: 2026-08-01

Track G PR-077 lands ADR-0006 §5.4 (cross-worker cancel). Today cancel is
purely in-process (``RunManager.cancel`` sets a local ``abort_event`` + cancels
the ``asyncio.Task``), so a cancel HTTP request that lands on a *different*
replica than the lease-holding worker silently no-ops. This revision adds the
**durable persisted cancel intent** the ADR mandates: a Gateway replica that
receives a cancel writes ``cancel_requested=true`` to PG (the durable source of
truth); the lease-holding worker polls it in its heartbeat loop and stops the
run. A Redis notify (published by ``RunManager.cancel``) accelerates delivery;
if the notify is lost, the worker still sees the intent from PG (§5.4 bullet 4).

Three additive columns (mirrors ``0017_run_row_version``'s NOT NULL +
server_default pattern for ``cancel_requested``; the other two are nullable):

* ``cancel_requested BOOLEAN NOT NULL DEFAULT FALSE`` — the durable intent flag.
  NOT NULL with ``server_default=false`` so existing rows are backfilled to
  "not cancelled" at ALTER time.
* ``cancel_action VARCHAR(16) NULL`` — ``interrupt`` / ``rollback`` (the action
  the worker takes on noticing the intent). Nullable: unset until a cancel is
  requested.
* ``cancel_requested_at DateTime(timezone=True) NULL`` — when the cancel was
  requested (observability / latency measurement). Nullable.

No CHECK constraint (``cancel_action`` is application-layer validated, same as
the ``status`` column convention — PR-070 §16.65). ``row_version`` is **not**
touched: the intent write is an independent ``UPDATE runs SET
cancel_requested=true WHERE run_id=:id AND status IN ('pending','running')``
(non-CAS; intent is a signal, not a terminal state — the cancel-vs-completion
race is still arbitrated by PR-070's terminal-status CAS).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from deerflow.persistence.migrations._helpers import safe_add_column, safe_drop_column

# revision identifiers, used by Alembic.
revision: str = "0018_run_cancel_intent"
down_revision: str | Sequence[str] | None = "0017_run_row_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOT NULL with server_default for cancel_requested (existing rows backfilled to
# false at ALTER time); nullable for the action + timestamp. Matches the ORM
# ``Mapped[...]`` declarations so create_all↔migrated parity holds
# (test_create_all_and_alembic_upgrade_produce_same_schema).
_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    (
        "cancel_requested",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    ),
    (
        "cancel_action",
        sa.Column("cancel_action", sa.String(length=16), nullable=True),
    ),
    (
        "cancel_requested_at",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    ),
)


def upgrade() -> None:
    """Add the persisted cancel-intent columns to ``runs``."""
    for _name, column in _COLUMNS:
        safe_add_column("runs", column)


def downgrade() -> None:
    """Drop the persisted cancel-intent columns from ``runs``."""
    for name, _column in reversed(_COLUMNS):
        safe_drop_column("runs", name)
