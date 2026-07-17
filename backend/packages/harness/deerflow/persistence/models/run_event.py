"""ORM model for run events."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class RunEventRow(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Owner of the conversation this event belongs to. Nullable for data
    # created before auth was introduced; populated by auth middleware on
    # new writes and by the boot-time orphan migration on existing rows.
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Tenant boundary for this event. Enforce NOT NULL landed in PR-025A
    # (revision 0006): the column is non-nullable after PR-023 backfill filled
    # every legacy row. FK RESTRICT keeps event history intact if an org is
    # hard-deleted.
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    # "message" | "trace" | "lifecycle"
    content: Mapped[str] = mapped_column(Text, default="")
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    seq: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"),
        Index("ix_events_thread_cat_seq", "thread_id", "category", "seq"),
        Index("ix_events_run", "thread_id", "run_id", "seq"),
        # Compatible (temporary) org_id-prefixed index for org-scoped event
        # queries (data-model.md §1 #9). Cleaned up at the Contract phase
        # (§13.4). Mirrors ix_events_run with an org_id prefix.
        Index("ix_events_org_thread_run", "org_id", "thread_id", "run_id"),
    )
