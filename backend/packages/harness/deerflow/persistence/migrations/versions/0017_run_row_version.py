"""Add ``row_version`` CAS column to ``runs`` (PR-070 / Track G Run state CAS).

Revision ID: 0017_run_row_version
Revises: 0016_run_release_pin
Create Date: 2026-07-31

Track G PR-070 lands the "freeze terminal / cancel / resume / reconcile
semantics" step (pr-split-guide.md §12). This revision adds a single
additive ``row_version`` column that is the compare-and-set token future
status writes key on — mirroring ``release_channels.row_version`` (PR-053)
and the ``_cas_update_channel`` idiom (``release/repository.py``).

A status write that carries an ``expected_row_version`` appends
``AND row_version = :expected`` to its ``WHERE`` clause and bumps
``row_version`` on success; ``rowcount == 0`` means a concurrent writer
won (a terminal completion racing a cancel), so the caller knows its
transition did not land (TM-027 mitigation at the store layer). Writes
that omit ``expected_row_version`` remain unconditional for backward
compatibility — only callers that opt into CAS observe it.

One additive **NOT NULL** column with ``server_default = 1``: every existing
row is backfilled to version 1 at ALTER time, and every new row inherits the
default. No data is lost; the column is a pure CAS primitive, never read for
its own sake outside the ``WHERE row_version = :expected`` predicate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from deerflow.persistence.migrations._helpers import safe_add_column, safe_drop_column

# revision identifiers, used by Alembic.
revision: str = "0017_run_row_version"
down_revision: str | Sequence[str] | None = "0016_run_release_pin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOT NULL with server_default "1": the ALTER stamps every existing row at
# version 1 (the CAS baseline), and every new row inherits the default. NOT NULL
# (not nullable) mirrors the ORM ``Mapped[int]`` with ``default=1``, which
# SQLAlchemy renders as NOT NULL — parity between create_all and the migration
# (test_create_all_and_alembic_upgrade_produce_same_schema).
_COLUMN: tuple[tuple[str, sa.Column], ...] = (
    (
        "row_version",
        sa.Column("row_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    ),
)


def upgrade() -> None:
    """Add the ``row_version`` CAS column to ``runs``."""
    for _name, column in _COLUMN:
        safe_add_column("runs", column)


def downgrade() -> None:
    """Drop the ``row_version`` CAS column from ``runs``."""
    for name, _column in reversed(_COLUMN):
        safe_drop_column("runs", name)
