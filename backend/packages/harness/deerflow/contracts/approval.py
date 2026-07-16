"""ApprovalTicket contract (MVP reservation).

Freezes the interrupt reference a Run may emit when a policy decision is
``require_approval`` (runtime-contracts.md §9). The MVP does **not** deliver a
full approval workflow, UI, or state machine. This contract only exists so the
runtime can produce a stable, safe interrupt reference and so consumers fail
closed when no approval adapter is wired (§9 MVP constraints).

Rules:

* ``resume_token_ref`` is a *reference*, never a reusable plaintext token;
* ``ask_clarification`` must never create an ApprovalTicket (§9);
* when no approval adapter is implemented, a ``require_approval`` decision keeps
  the Run in a safe terminal or non-recoverable-wait state — it is never
  treated as ``allow`` (§7.4, §9).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deerflow.contracts.versioning import CURRENT_SCHEMA_VERSION

ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]
"""Closed approval lifecycle set."""


class ApprovalTicket(BaseModel):
    """Stable interrupt reference for a require_approval decision.

    MVP only: this is a frozen reference. The full approval service, UI and
    multi-stage state machine are explicitly out of scope (§9, §15).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str = Field(default=CURRENT_SCHEMA_VERSION)
    ticket_id: str = Field(min_length=1, description="Ticket id; never empty.")
    org_id: str = Field(min_length=1, description="Owning org; never empty.")
    run_id: str = Field(min_length=1, description="Associated run; never empty.")
    action: str = Field(min_length=1, description="Action awaiting approval.")
    risk_class: str = Field(description="Risk class of the action (free string per §9 schema).")
    status: ApprovalStatus = Field(description="Approval lifecycle state.")
    resume_token_ref: str = Field(
        min_length=1,
        description="Reference to a resume token; never a reusable plaintext token.",
    )
    created_at: datetime = Field(description="When the ticket was created (UTC).")
    expires_at: datetime = Field(description="When the ticket expires (UTC).")

    @field_validator("created_at", "expires_at", mode="before")
    @classmethod
    def _normalize_utc(cls, value: object) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise TypeError("datetime must be an RFC 3339 datetime")
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return value.astimezone(UTC)
