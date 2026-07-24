"""Restore an application-level snapshot into an empty DB (PR-065).

``restore_from_manifest`` reloads the snapshot's normalised rows into a
target DB. The contract is the DR scenario from runbook §10.2: the primary
DB is lost, the operator points the gateway at a fresh empty DB, and the
restore repopulates it from the backup point. It is **not** a merge into an
existing DB — the target must be empty, and restore asserts that up front
(``_assert_target_empty``) so a mis-targeted restore cannot silently mix a
backup point with live data.

The restore writes rows in metadata dependency order (parent tables before
FK children — the same order ``snapshot.take_snapshot`` recorded), batched
per table. Values are written exactly as snapshotted (backend-neutral JSON
round-trips through Core insert), so a byte-faithful restore reproduces the
snapshot's content digests (the property ``verify.verify_restore`` checks).

Schema provisioning: the restore assumes the target schema exists. In
practice the operator runs ``alembic upgrade head`` (or the gateway's
create-all bootstrap) on the empty target first; restore then only fills
rows. We deliberately do NOT run create_all here — mixing DDL into a data
restore violates pr-split-guide §14 ("不与数据库业务迁移混合") and would
mask schema drift from the manifest's recorded head.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import DateTime, LargeBinary, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.types import TypeEngine

from deerflow.persistence.backup.manifest import BackupManifest, BackupTableEntry
from deerflow.persistence.base import Base

logger = logging.getLogger(__name__)

#: Rows committed per INSERT batch. Bounded so a single batch is short and
#: the restore stays interruptible; large enough to avoid per-row commit
#: overhead on the bigger control-plane tables.
_WRITE_BATCH_SIZE = 1000

#: Subdirectory inside ``destination_dir`` holding the per-table snapshot
#: content files. Kept separate from ``manifest.json`` so an operator can
#: move/encrypt the content files independently of the manifest.
SNAPSHOT_CONTENT_DIR = "snapshot"


def _coerce_for_column(value: Any, col_type: TypeEngine) -> Any:
    """Convert a snapshotted (JSON-serialised) value back to its Python type.

    The snapshot normalises values for a stable digest: datetimes → ISO
    strings, bytes → hex. SQLite's DateTime column rejects bare ISO strings
    (it wants ``datetime`` objects), and a LargeBinary column wants bytes.
    This is the inverse of ``snapshot._normalise_value``: it maps the
    JSON form back to the Python object the column's bind processor expects.

    JSON columns (``JSON`` / ``JSONB``) accept dict/list as-is. String/Int/
    Bool/UUID-as-String columns accept the JSON scalar directly. Unknown
    types pass through (the snapshot stored the native value).
    """
    if value is None:
        return None
    # DateTime: ISO string (tz-aware) → datetime. ``fromisoformat`` handles
    # the ``+00:00`` offset the snapshot writes; a naive string is parsed
    # as-is (the snapshot only writes tz-aware, but be robust).
    if isinstance(col_type, DateTime) and isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    # LargeBinary: hex string → bytes.
    if isinstance(col_type, LargeBinary) and isinstance(value, str):
        try:
            return bytes.fromhex(value)
        except ValueError:
            return value
    return value


class RestoreError(Exception):
    """Raised when a restore cannot proceed (non-empty target, missing content)."""


class RestoreReport(BaseModel):
    """Result of a restore: per-table rows written + an integrity flag.

    The integrity flag is True iff every table's written row count matched
    its manifest entry. It is NOT a content-digest check — that runs in
    ``verify.verify_restore`` against the restored DB, the only place that
    can recompute the snapshot digest from live rows.
    """

    restored_counts: dict[str, int]
    integrity_ok: bool
    tables_in_order: list[str]

    model_config = {"extra": "forbid"}


def _snapshot_content_file(content_dir: Path, table_name: str) -> Path:
    return Path(content_dir) / SNAPSHOT_CONTENT_DIR / f"{table_name}.jsonl"


def _iter_snapshot_rows(path: Path) -> list[dict[str, Any]]:
    """Read a ``.jsonl`` snapshot file into a list of row dicts.

    Each line is one normalised row object (the format ``snapshot`` wrote).
    Blank lines are tolerated (a trailing newline should not be a hard
    error).
    """
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


async def _assert_target_empty(session: AsyncSession) -> None:
    """Fail closed if any DeerFlow table already has rows.

    A merge into a live DB would corrupt both the backup point and the live
    data; the DR restore contract is restore-to-empty. Returns the first
    non-empty table in the error message so the operator knows what to clear.
    """
    import deerflow.persistence.models  # noqa: F401

    for table in Base.metadata.sorted_tables:
        result = await session.execute(select(table).limit(1))
        if result.first() is not None:
            raise RestoreError(f"target DB is not empty: table '{table.name}' already has rows. Restore requires an empty target (DR scenario); clear the target DB or point at a fresh one.")


async def _write_table_rows(
    session: AsyncSession,
    entry: BackupTableEntry,
    rows: list[dict[str, Any]],
) -> int:
    """Insert ``rows`` into ``entry.name`` in batches; return count written."""
    table = Base.metadata.tables[entry.name]
    # Pre-resolve each column's TypeEngine once (per-table, not per-row) so
    # the per-value coercion is a cheap isinstance check rather than a
    # metadata lookup for every cell of every row.
    col_types = {name: table.c[name].type for name in entry.columns if name in table.c}
    written = 0
    for start in range(0, len(rows), _WRITE_BATCH_SIZE):
        batch = rows[start : start + _WRITE_BATCH_SIZE]
        if not batch:
            continue
        # Only include columns the manifest recorded; a restore into a newer
        # schema (extra column) tolerates the missing key, a restore into an
        # older schema (dropped column) is a schema-drift the verify step
        # flags separately. Coerce JSON-serialised values back to the Python
        # types the column bind processors expect (datetimes, bytes).
        payload = [{k: _coerce_for_column(row.get(k), col_types[k]) for k in entry.columns if k in col_types} for row in batch]
        await session.execute(table.insert(), payload)
        written += len(batch)
    return written


async def restore_from_manifest(
    sf: async_sessionmaker,
    manifest: BackupManifest,
    content_dir: Path,
) -> RestoreReport:
    """Reload the snapshot into the target DB (must be empty).

    ``content_dir`` is the ``destination_dir`` the backup Job wrote to; the
    snapshot rows live under ``content_dir/snapshot/<table>.jsonl``. Tables
    are restored in manifest order (metadata dependency order), so parent
    rows land before FK children.
    """
    import deerflow.persistence.models  # noqa: F401

    restored: dict[str, int] = {}
    integrity = True
    order: list[str] = []

    async with sf() as session:
        await _assert_target_empty(session)
        for entry in manifest.tables:
            order.append(entry.name)
            content_file = _snapshot_content_file(content_dir, entry.name)
            if not content_file.exists():
                # A missing content file is a corrupted backup — fail closed
                # before writing any rows so the target stays clean.
                raise RestoreError(f"snapshot content file missing for table '{entry.name}': {content_file}")
            rows = _iter_snapshot_rows(content_file)
            written = await _write_table_rows(session, entry, rows)
            restored[entry.name] = written
            if written != entry.row_count:
                integrity = False
                logger.error(
                    "restore row-count mismatch for %s: wrote %d, manifest expects %d",
                    entry.name,
                    written,
                    entry.row_count,
                )
        await session.commit()

    return RestoreReport(restored_counts=restored, integrity_ok=integrity, tables_in_order=order)


__all__ = [
    "SNAPSHOT_CONTENT_DIR",
    "RestoreError",
    "RestoreReport",
    "restore_from_manifest",
]
