"""ORM model for run metadata."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Tenant boundary for this run. Enforce NOT NULL landed in PR-025A
    # (revision 0006): the column is non-nullable after PR-023 backfill filled
    # every legacy row. FK RESTRICT keeps run history intact if an org is
    # hard-deleted (orgs normally soft-delete via deleted_at).
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    assistant_id: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # "pending" | "running" | "success" | "error" | "timeout" | "interrupted"

    model_name: Mapped[str | None] = mapped_column(String(128))
    multitask_strategy: Mapped[str] = mapped_column(String(20), default="reject")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    kwargs_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    # Convenience fields (for listing pages without querying RunEventStore)
    message_count: Mapped[int] = mapped_column(default=0)
    first_human_message: Mapped[str | None] = mapped_column(Text)
    last_ai_message: Mapped[str | None] = mapped_column(Text)

    # Token usage (accumulated in-memory by RunJournal, written on run completion)
    total_input_tokens: Mapped[int] = mapped_column(default=0)
    total_output_tokens: Mapped[int] = mapped_column(default=0)
    total_tokens: Mapped[int] = mapped_column(default=0)
    llm_call_count: Mapped[int] = mapped_column(default=0)
    lead_agent_tokens: Mapped[int] = mapped_column(default=0)
    subagent_tokens: Mapped[int] = mapped_column(default=0)
    middleware_tokens: Mapped[int] = mapped_column(default=0)
    token_usage_by_model: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))

    # Follow-up association
    follow_up_to_run_id: Mapped[str | None] = mapped_column(String(64))

    # ReleaseRef pin (PR-056 / ADR-0004 §6 step 7). Frozen at run creation so
    # the execution phase consumes the persisted identity and never drifts on a
    # later promote/rollback (ADR §6 step 9). All nullable: a run is "legacy"
    # when these are unset. No FK — the digest is the integrity guarantee, not
    # a relational join (mirrors release_idempotency_records, revision 0015).
    release_package_id: Mapped[str | None] = mapped_column(String(36))
    release_version_id: Mapped[str | None] = mapped_column(String(36))
    release_channel: Mapped[str | None] = mapped_column(String(16))
    release_digest: Mapped[str | None] = mapped_column(String(96))
    # Marks runs created before/without release enforcement (ADR §12). Defaults
    # true (server_default in migration 0016) so existing rows become legacy at
    # ALTER time and new rows stay legacy until start_run pins them. NOT NULL to
    # match the migration (test_create_all_and_alembic_upgrade_produce_same_schema).
    legacy_unpinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ix_runs_thread_status", "thread_id", "status"),
        # Compatible (temporary) org_id-prefixed indexes for org-scoped run
        # queries (data-model.md §1 #9, §7.2). Cleaned up at the Contract
        # phase (§13.4).
        Index("ix_runs_org_status_created", "org_id", "status", "created_at"),
        Index("ix_runs_org_thread_created", "org_id", "thread_id", "created_at"),
    )
