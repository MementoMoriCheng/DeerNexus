"""ORM model for user feedback on runs."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class FeedbackRow(Base):
    __tablename__ = "feedback"

    __table_args__ = (
        UniqueConstraint("thread_id", "run_id", "user_id", name="uq_feedback_thread_run_user"),
        # Compatible (temporary) org_id-prefixed index for org-scoped feedback
        # queries (data-model.md §1 #9). Cleaned up at the Contract phase
        # (§13.4).
        Index("ix_feedback_org_thread", "org_id", "thread_id"),
    )

    feedback_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Tenant boundary for this feedback. Enforce NOT NULL landed in PR-025A
    # (revision 0006): the column is non-nullable after PR-023 backfill filled
    # every legacy row. FK RESTRICT keeps feedback intact if an org is
    # hard-deleted.
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    message_id: Mapped[str | None] = mapped_column(String(64))
    # message_id is an optional RunEventStore event identifier —
    # allows feedback to target a specific message or the entire run

    rating: Mapped[int] = mapped_column(nullable=False)
    # +1 (thumbs-up) or -1 (thumbs-down)

    comment: Mapped[str | None] = mapped_column(Text)
    # Optional text feedback from the user

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
