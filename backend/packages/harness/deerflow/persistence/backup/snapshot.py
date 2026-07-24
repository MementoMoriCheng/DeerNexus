"""Application-level logical snapshot of every DeerFlow table (PR-065).

``take_snapshot`` reads every table registered on ``Base.metadata`` via
SQLAlchemy Core (no ORM hydration per row), normalises each row to a
backend-neutral JSON value, and records a sha256 ``content_digest`` per
table. The snapshot is the content the manifest (``backup.manifest``)
describes; together they are the backup's evidence layer (see the
``backup.manifest`` module docstring for why this is evidence, not a dump).

Scope: this is the **application's** view of its own tables — everything on
``Base.metadata``. LangGraph's checkpointer tables are intentionally
excluded (they are not registered on this Base; the run lifecycle
tables DeerFlow owns — ``threads_meta`` / ``runs`` / ``run_events`` — are
the authoritative run state and ARE snapshotted). The snapshot never
mutates the source DB (read-only ``SELECT``), so it is safe to run on a
live gateway.

Determinism (cross-backend + cross-rerun)
------------------------------------------

Rows are sorted by primary key (falling back to full row tuple) so the
digest is stable for the same data regardless of physical row order,
storage backend, or snapshot rerun. Column values are normalised:

* ``datetime`` → ISO-8601 UTC (``datetime.isoformat``; naive datetimes are
  assumed UTC, matching the harness convention);
* ``bytes`` / ``bytea`` → hex;
* everything else passes through JSON's default serialiser.

The same normalisation runs on restore write-back, so a byte-faithful
restore reproduces the exact content digest (locked by
``test_backup_restore``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.backup.manifest import (
    BackupManifest,
    BackupTableEntry,
    finalize_digests,
)
from deerflow.persistence.base import Base

logger = logging.getLogger(__name__)

#: Batch size for reading large tables. Core streaming would be ideal, but
#: the snapshot target is MVP-scale DBs; a bounded fetch keeps memory
#: predictable without a per-dialect cursor dance. The digest is over the
#: full sorted row stream regardless of batch boundaries.
_READ_BATCH_SIZE = 5000


def _normalise_value(value: Any) -> Any:
    """Backend-neutral JSON value for a single cell (see module docstring)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    # UUID objects serialise cleanly via default=str; cover explicitly so a
    # custom default handler is not surprised by them.
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _normalise_row(row: Any, column_keys: tuple[str, ...]) -> dict[str, Any]:
    """Project a Core row to a ``{column: normalised_value}`` dict in order.

    Core ``Row`` objects expose a ``._mapping`` (column-name → value); reading
    through it is stable across positional vs labelled selects. Missing keys
    (a column dropped between snapshot and a future schema) default to None
    rather than raising so a partial snapshot still digests.
    """
    mapping = row._mapping  # type: ignore[attr-defined]
    return {key: _normalise_value(mapping[key] if key in mapping else None) for key in column_keys}


def _stable_row_payload(rows: list[dict[str, Any]]) -> str:
    """Deterministic JSON over the (already PK-sorted) row list."""
    return json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _content_digest_for_rows(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_stable_row_payload(rows).encode("utf-8")).hexdigest()


def _sorted_table_names() -> list[str]:
    """DeerFlow-owned table names in metadata's dependency (create) order.

    Sorted by metadata dependency so a restore can insert in the same order
    without violating FK constraints; the snapshot records this order in the
    manifest so the restore does not re-derive it.
    """
    return [t.name for t in Base.metadata.sorted_tables]


def _table_column_keys(table_name: str) -> tuple[str, ...]:
    """Mapper-order column keys for ``table_name`` from ``Base.metadata``."""
    table = Base.metadata.tables[table_name]
    return tuple(c.key for c in table.columns)


async def _read_alembic_head(session: AsyncSession) -> str:
    """Read the ``alembic_version.version_num`` head row, or ``"unknown"``.

    The snapshot records the schema point it was taken against so a restore
    into a DB whose alembic head has drifted fails closed in verify. A DB
    that pre-dates alembic (no ``alembic_version`` table) returns
    ``"unknown"`` — the verify step treats that as a schema-drift FAIL, which
    is correct: a snapshot of an unversioned DB cannot be verified against a
    versioned target.
    """
    try:
        result = await session.execute(text("SELECT version_num FROM alembic_version"))
        row = result.first()
        if row is None:
            return "unknown"
        return str(row[0])
    except Exception:  # noqa: BLE001 — table missing / unreadable
        return "unknown"


async def _snapshot_table(
    session: AsyncSession,
    table_name: str,
    column_keys: tuple[str, ...],
    *,
    pk_fallback_order: tuple[str, ...] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Read all rows of one table, PK-sorted, returning (rows, count)."""
    table = Base.metadata.tables[table_name]
    order_cols = [table.c[name] for name in pk_fallback_order] if pk_fallback_order else list(table.primary_key.columns)
    stmt = select(table)
    if order_cols:
        stmt = stmt.order_by(*order_cols)
    else:
        # No PK defined (none of our tables fall here, but be robust): order
        # by the full column set so the digest is still deterministic.
        stmt = stmt.order_by(*list(table.columns))
    result = await session.execute(stmt)
    rows = [_normalise_row(row, column_keys) for row in result.fetchall()]
    return rows, len(rows)


async def read_table_rows(
    sf: async_sessionmaker,
    table_name: str,
) -> list[dict[str, Any]]:
    """Read one table's rows (PK-sorted, normalised) for content-file writing.

    Shared between the backup CLI (writes the per-table ``.jsonl`` content
    file) and tests. Re-reads rather than reusing the snapshot pass so the
    CLI can stream large tables without holding every table's rows in memory
    at once — only the current table's rows are materialised.
    """
    import deerflow.persistence.models  # noqa: F401

    column_keys = _table_column_keys(table_name)
    pk_cols = tuple(c.key for c in Base.metadata.tables[table_name].primary_key.columns)
    async with sf() as session:
        rows, _ = await _snapshot_table(session, table_name, column_keys, pk_fallback_order=pk_cols or None)
    return rows


def write_table_rows(content_dir: str | Path, table_name: str, rows: list[dict[str, Any]]) -> Path:
    """Write one table's rows to ``content_dir/snapshot/<table>.jsonl``.

    One JSON object per line (``.jsonl``) so the file is streamable and a
    single corrupted line does not invalidate the whole file. Atomic via
    write-then-rename; the snapshot subdirectory is created on demand.
    """
    import json

    content_dir = Path(content_dir)
    snapshot_dir = content_dir / "snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    target = snapshot_dir / f"{table_name}.jsonl"
    tmp = target.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str))
            f.write("\n")
    tmp.replace(target)
    return target


async def take_snapshot(
    sf: async_sessionmaker,
    *,
    backend: str,
    declared_rpo_hours: int,
    now: datetime | None = None,
) -> BackupManifest:
    """Snapshot every DeerFlow table; return a finalised manifest.

    Read-only against the source DB. ``backend`` is recorded in the manifest
    so a cross-backend restore is surfaced to the operator (the snapshot is
    backend-neutral, but the operator should know a postgres snapshot was
    restored into sqlite, e.g.).
    """
    if now is None:
        now = datetime.now(UTC)

    # Ensure every ORM model is registered (mirrors bootstrap.py); a missing
    # import would silently drop tables from Base.metadata.
    import deerflow.persistence.models  # noqa: F401

    entries: list[BackupTableEntry] = []
    async with sf() as session:
        schema_version = await _read_alembic_head(session)
        for table_name in _sorted_table_names():
            column_keys = _table_column_keys(table_name)
            pk_cols = tuple(c.key for c in Base.metadata.tables[table_name].primary_key.columns)
            rows, count = await _snapshot_table(session, table_name, column_keys, pk_fallback_order=pk_cols or None)
            digest = _content_digest_for_rows(rows)
            entries.append(
                BackupTableEntry(
                    name=table_name,
                    row_count=count,
                    content_digest=digest,
                    columns=list(column_keys),
                )
            )

    manifest = BackupManifest(
        created_at=now,
        backend=backend,
        schema_version=schema_version,
        declared_rpo_hours=declared_rpo_hours,
        tables=entries,
    )
    return finalize_digests(manifest)


__all__ = [
    "read_table_rows",
    "take_snapshot",
    "write_table_rows",
]
