"""ORM model for thread metadata."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class ThreadMetaRow(Base):
    __tablename__ = "threads_meta"
    __table_args__ = (
        # Compatible (temporary) org_id-prefixed listing index for org-scoped
        # thread queries (data-model.md §1 #9, §7.1). Cleaned up at the
        # Contract phase (§13.4). workspace_id intentionally omitted (PR-024).
        Index("ix_threads_meta_org_updated", "org_id", "updated_at"),
    )

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Tenant boundary for this thread (PR-021 Expand: nullable so legacy rows
    # remain NULL until PR-023 backfill). FK RESTRICT: a thread must not be
    # lost if its owning org is hard-deleted (orgs normally soft-delete via
    # deleted_at). Enforce NOT NULL lands in PR-025A.
    org_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    assistant_id: Mapped[str | None] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    display_name: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(20), default="idle")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))
