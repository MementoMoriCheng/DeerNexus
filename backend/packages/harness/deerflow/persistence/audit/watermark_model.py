"""ORM model for the ``audit_archive_watermarks`` table (T1C).

Operational metadata for the ADR-0005 §10.2 archive job. Unlike
``audit_events`` (append-only with a ``BEFORE UPDATE OR DELETE`` trigger),
this table is freely updatable — the archive job writes the watermark after
each successful batch so the next sweep resumes from the right position.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditArchiveWatermarkRow(Base):
    """Watermark tracking the last-archived position per Org.

    ``org_id`` is the PK. The special value ``"system"`` represents
    cross-Org / system-level audit events that are not scoped to any Org.

    ``last_archived_window_end`` is the exclusive upper bound of the last
    successfully archived window. Events with ``occurred_at <
    last_archived_window_end`` for this Org have been written to object
    storage. ``last_event_id`` breaks ties for events sharing the same
    timestamp (the stable ``(occurred_at, event_id)`` cursor order).

    ``batch_count`` accumulates the total number of archive batches written
    for this Org — an observability counter for capacity planning.
    """

    __tablename__ = "audit_archive_watermarks"

    org_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    last_archived_window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    batch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)
