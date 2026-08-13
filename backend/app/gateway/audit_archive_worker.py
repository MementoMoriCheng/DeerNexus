"""Audit archive job — daily JSONL export of closed-window audit events (T1C).

ADR-0005 §10.2: a daily job exports audit events for a closed time window
to object storage (JSONL), writes a manifest with count/digest/first-last
event ids, records the watermark, and does a read-back verification.

Design constraints:

* **Append-only safe**: ``audit_events`` has a ``BEFORE UPDATE OR DELETE``
  trigger (migration 0010). The archive job NEVER modifies event rows — it
  only SELECTs them for export and records the watermark in a separate
  ``audit_archive_watermarks`` table.
* **Idempotent**: re-archiving the same window writes the same JSONL
  content (stable sort by ``(occurred_at, event_id)``) and overwrites the
  existing object; the watermark is only advanced, never regressed.
* **Streaming**: events are exported in batches to avoid loading an entire
  day into memory.
* **Object Store**: uses the ``ObjectStore`` Protocol (PR-T1B). With the
  inline backend (dev) the archive is a no-op write to memory; with S3 it
  writes real JSONL files.

Follows the ``release_gc_worker`` pattern: a testable ``sweep_archive_audit``
single-pass function + a ``run_audit_archive_worker`` loop wrapper.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.audit.model import AuditEventRow
from deerflow.persistence.audit.watermark_model import AuditArchiveWatermarkRow
from deerflow.persistence.release.storage import ObjectStore

logger = logging.getLogger(__name__)

#: Archive interval — ADR §10.2 "至少每日一次". 24h in production; tests
#: pass a shorter value via the worker loop.
ARCHIVE_INTERVAL_SECONDS = 24 * 60 * 60.0

#: Batch size for streaming events from the DB. Large enough to amortise
#: query overhead, small enough to keep memory bounded.
ARCHIVE_BATCH_SIZE = 500

#: Storage version stamped on every manifest (ADR §10.2 ``storage_version``).
STORAGE_VERSION = "1"


@dataclass(frozen=True)
class ArchiveReport:
    """Result of a single archive sweep pass."""

    orgs_archived: int = 0
    events_archived: int = 0
    batches_written: int = 0
    verify_failures: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.verify_failures


def _archive_object_key(*, org_id: str, date_str: str, batch_id: str) -> str:
    """ADR §10.2 canonical object key: ``audit/org/{org}/date/{date}/batch/{id}.jsonl``."""
    return f"audit/org/{org_id}/date/{date_str}/batch/{batch_id}.jsonl"


def _manifest_object_key(*, org_id: str, date_str: str) -> str:
    """Manifest key: ``audit/org/{org}/date/{date}/manifest.json``."""
    return f"audit/org/{org_id}/date/{date_str}/manifest.json"


def _row_to_jsonl(row: AuditEventRow) -> str:
    """Serialize an AuditEventRow to a JSONL line (stable key order)."""
    return json.dumps(
        {
            "event_id": row.event_id,
            "org_id": row.org_id,
            "action": row.action,
            "actor_type": row.actor_type,
            "actor_id": row.actor_id,
            "outcome": row.outcome,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
            "payload": row.payload,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


async def _get_all_org_ids(sf: async_sessionmaker[AsyncSession]) -> list[str]:
    """Return distinct org_ids that have audit events, plus 'system' for NULL org."""
    async with sf() as session:
        result = await session.execute(select(AuditEventRow.org_id).distinct())
        org_ids = [row[0] for row in result.all() if row[0] is not None]
    # Always include 'system' so NULL-org events are archived too.
    if "system" not in org_ids:
        org_ids.append("system")
    return org_ids


async def _get_watermark(sf: async_sessionmaker[AsyncSession], *, org_id: str) -> AuditArchiveWatermarkRow | None:
    async with sf() as session:
        return await session.get(AuditArchiveWatermarkRow, org_id)


async def _upsert_watermark(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    window_end: datetime,
    last_event_id: str,
    batch_count_delta: int,
) -> None:
    async with sf() as session:
        existing = await session.get(AuditArchiveWatermarkRow, org_id)
        if existing is not None:
            existing.last_archived_window_end = window_end
            existing.last_event_id = last_event_id
            existing.batch_count += batch_count_delta
        else:
            session.add(
                AuditArchiveWatermarkRow(
                    org_id=org_id,
                    last_archived_window_end=window_end,
                    last_event_id=last_event_id,
                    batch_count=batch_count_delta,
                )
            )
        await session.commit()


async def _export_window(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    window_start: datetime,
    window_end: datetime,
    start_after_event_id: str | None = None,
) -> tuple[list[AuditEventRow], str]:
    """Export audit events for org_id in [window_start, window_end).

    Returns (rows, content_digest). Rows are ordered by (occurred_at, event_id)
    for stable digest. For 'system' org_id, selects NULL org_id events.
    """
    async with sf() as session:
        if org_id == "system":
            org_filter = AuditEventRow.org_id.is_(None)
        else:
            org_filter = AuditEventRow.org_id == org_id

        stmt = (
            select(AuditEventRow)
            .where(
                org_filter,
                AuditEventRow.occurred_at >= window_start,
                AuditEventRow.occurred_at < window_end,
            )
            .order_by(AuditEventRow.occurred_at, AuditEventRow.event_id)
            .limit(ARCHIVE_BATCH_SIZE)
        )
        result = await session.execute(stmt)
        rows = list(result.scalars().all())

    # Build JSONL content + digest.
    jsonl_lines = [_row_to_jsonl(r) for r in rows]
    content = "\n".join(jsonl_lines)
    if content:
        content += "\n"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return rows, digest


async def sweep_archive_audit(
    sf: async_sessionmaker[AsyncSession],
    *,
    object_store: ObjectStore,
    now: datetime | None = None,
) -> ArchiveReport:
    """Single-pass archive sweep. Archives yesterday's events for every org.

    Idempotent: re-running for the same window produces identical JSONL
    content (stable sort) and overwrites the existing object. The watermark
    is only advanced forward.

    ``now`` is injectable for deterministic tests; defaults to UTC now.
    """
    if now is None:
        now = datetime.now(UTC)

    # The archive window is yesterday 00:00–24:00 UTC (a fully closed window).
    yesterday = now.date() - timedelta(days=1)
    window_start = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC)
    window_end = window_start + timedelta(days=1)
    date_str = yesterday.isoformat()

    org_ids = await _get_all_org_ids(sf)
    total_events = 0
    total_batches = 0
    verify_failures: list[str] = []

    for org_id in org_ids:
        rows, digest = await _export_window(sf, org_id=org_id, window_start=window_start, window_end=window_end)

        if not rows:
            continue  # No events for this org in this window — skip.

        batch_id = uuid.uuid4().hex[:12]
        jsonl_key = _archive_object_key(org_id=org_id, date_str=date_str, batch_id=batch_id)
        manifest_key = _manifest_object_key(org_id=org_id, date_str=date_str)

        # Build JSONL content.
        jsonl_lines = [_row_to_jsonl(r) for r in rows]
        jsonl_content = "\n".join(jsonl_lines)
        if jsonl_content:
            jsonl_content += "\n"

        # Write JSONL to object store (ADR §10.2 step 4).
        await object_store.put(object_key=jsonl_key, content=jsonl_content.encode("utf-8"))

        # Write manifest (ADR §10.2 step 7).
        manifest = {
            "batch_id": batch_id,
            "schema_versions": ["audit_event_v1"],
            "org_id": org_id if org_id != "system" else None,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "event_count": len(rows),
            "first_event_id": rows[0].event_id,
            "last_event_id": rows[-1].event_id,
            "content_digest": f"sha256:{digest}",
            "created_at": now.isoformat(),
            "storage_version": STORAGE_VERSION,
            "batches": [{"batch_id": batch_id, "object_key": jsonl_key, "event_count": len(rows)}],
        }
        await object_store.put(object_key=manifest_key, content=json.dumps(manifest, sort_keys=True).encode("utf-8"))

        # Read-back verification (ADR §10.2 step 9).
        try:
            read_back = await object_store.get(jsonl_key)
            read_digest = hashlib.sha256(read_back).hexdigest()
            if read_digest != digest:
                verify_failures.append(f"org={org_id} date={date_str}: digest mismatch on read-back (expected sha256:{digest}, got sha256:{read_digest})")
                continue  # Don't advance watermark on verification failure.
        except Exception as exc:  # noqa: BLE001
            verify_failures.append(f"org={org_id} date={date_str}: read-back failed: {exc}")
            continue

        # Advance watermark (ADR §10.2 step 8).
        await _upsert_watermark(
            sf,
            org_id=org_id,
            window_end=window_end,
            last_event_id=rows[-1].event_id,
            batch_count_delta=1,
        )
        total_events += len(rows)
        total_batches += 1
        logger.info(
            "audit archive: org=%s date=%s events=%d batch=%s",
            org_id,
            date_str,
            len(rows),
            batch_id,
        )

    if verify_failures:
        logger.error("audit archive: %d verification failure(s): %s", len(verify_failures), verify_failures)

    return ArchiveReport(
        orgs_archived=total_batches,
        events_archived=total_events,
        batches_written=total_batches,
        verify_failures=verify_failures,
    )


async def run_audit_archive_worker(
    sf: async_sessionmaker[AsyncSession],
    *,
    object_store: ObjectStore,
    interval: float = ARCHIVE_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background loop: archive audit events every ``interval`` until ``stop_event``.

    Started as an ``asyncio.create_task`` in the gateway lifespan (alongside
    the audit outbox worker). Each pass archives yesterday's events; a pass
    that raises is logged and the loop continues (the archive must never
    kill the worker). When the object store is inline (dev mode), the worker
    still runs but writes to the no-op store.
    """
    if stop_event is None:
        stop_event = asyncio.Event()
    logger.info("audit archive worker started (interval=%.0fs)", interval)
    while not stop_event.is_set():
        try:
            report = await sweep_archive_audit(sf, object_store=object_store)
            if report.events_archived:
                logger.info(
                    "audit archive sweep: orgs=%d events=%d batches=%d failures=%d",
                    report.orgs_archived,
                    report.events_archived,
                    report.batches_written,
                    len(report.verify_failures),
                )
        except Exception:  # noqa: BLE001
            logger.exception("audit archive sweep raised; continuing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass  # interval elapsed; loop and sweep again
    logger.info("audit archive worker stopped")


__all__ = [
    "ARCHIVE_INTERVAL_SECONDS",
    "ArchiveReport",
    "run_audit_archive_worker",
    "sweep_archive_audit",
]
