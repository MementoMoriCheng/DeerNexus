"""Audit and usage event contracts.

Freezes the compliance evidence (``AuditEvent``) and metering fact
(``UsageRecord``) DTOs, plus the harness-facing sink Protocols
(runtime-contracts.md §10–§11, ADR-0005).

Security rules:

* ``AuditEvent.org_id`` is required for tenant events; ``None`` is allowed only
  for system-global events on the documented system allow-list (ADR-0002 §4.1);
* ``payload`` follows a per-action allow-list schema (ADR-0005 §6). It must
  never carry secrets, tokens, full prompts, full model responses, full file
  contents, or signed-URL query strings. The DTO cannot enforce the full
  per-action schema here (that needs the action registry), but it normalizes
  known forbidden keys out of the serialized surface as defense-in-depth;
* ``AuditEvent.event_id`` and ``idempotency_key`` are non-empty so retries are
  deduplicated by event identity;
* ``UsageRecord`` token fields are non-negative integers sourced from the model
  adapter — never client-submitted. ``cost_*`` may be ``None`` when no price
  table is available, but tokens must never be lost (§11).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deerflow.contracts.identity import PrincipalRef
from deerflow.contracts.policy import ResourceRef
from deerflow.contracts.versioning import CURRENT_SCHEMA_VERSION

AuditOutcome = Literal["success", "denied", "failure"]
"""Closed audit outcome set."""

UsageStatus = Literal["success", "failure", "cancelled"]
"""Closed usage status set."""

# Defense-in-depth: keys that must never appear in an audit payload (ADR-0005 §6).
# The DTO strips these from the serialized surface so a careless producer cannot
# leak a credential even if it places one in the dict. Per-action schema
# validation (action registry) is a later PR.
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "authorization_header",
        "cookie",
        "api_key",
        "key_hash",
        "oauth_token",
        "connector_password",
        "full_prompt",
        "full_model_response",
        "full_file_content",
        "signed_url_query",
        "database_dsn",
    }
)


def _scrub_payload(payload: dict) -> dict:
    """Return a copy of ``payload`` with forbidden secret-bearing keys removed.

    Per-action schema validation happens in the audit service; this is a
    belt-and-braces guard at the DTO boundary so a leaked key never reaches the
    serialized event.
    """
    return {k: v for k, v in payload.items() if k not in _FORBIDDEN_PAYLOAD_KEYS}


class AuditEvent(BaseModel):
    """Append-only compliance evidence (ADR-0005 §3).

    ``org_id`` is ``None`` only for documented system-global events
    (ADR-0002 §4.1); tenant events must set it. ``occurred_at`` is when the
    event happened; ingestion/ingested_at is tracked by the store.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str = Field(default=CURRENT_SCHEMA_VERSION)
    event_id: str = Field(min_length=1, description="Globally unique event id; never empty.")
    idempotency_key: str = Field(min_length=1, description="Producer-stable idempotency key; never empty.")
    org_id: str | None = Field(
        default=None,
        description="Owning org; required for tenant events, None only for system-global events.",
    )
    workspace_id: str | None = Field(default=None, description="Optional workspace within org_id.")
    actor: PrincipalRef = Field(description="Actor attributed to the event (durable id, not display name).")
    action: str = Field(min_length=1, description="Action in '<domain>.<resource>.<verb>' form.")
    resource: ResourceRef | None = Field(default=None, description="Affected resource, if any.")
    outcome: AuditOutcome = Field(description="success | denied | failure.")
    reason_code: str | None = Field(default=None, description="Stable reason code; None when not applicable.")
    request_id: str = Field(min_length=1, description="Correlation id; never empty.")
    trace_id: str | None = Field(default=None, description="Optional distributed trace id.")
    run_id: str | None = Field(default=None, description="Associated run, if any.")
    occurred_at: datetime = Field(description="When the event occurred (UTC).")
    payload: dict = Field(
        default_factory=dict,
        description="Per-action allow-listed payload; never secrets/prompts/tokens/content.",
    )

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _occurred_at_utc(cls, value: object) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise TypeError("occurred_at must be an RFC 3339 datetime")
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @field_validator("payload")
    @classmethod
    def _strip_forbidden_keys(cls, value: dict) -> dict:
        scrubbed = _scrub_payload(value)
        if len(scrubbed) != len(value):
            dropped = set(value) - set(scrubbed)
            raise ValueError(f"audit payload contains forbidden secret-bearing keys: {sorted(dropped)}")
        return scrubbed


class UsageRecord(BaseModel):
    """Metering fact for a single model invocation attempt (§11).

    Tokens come from the model adapter, never the client. ``org_id`` is
    inherited from the RunEnvelope. Provider retries produce multiple records
    distinguished by ``attempt``; aggregation deduplicates to avoid double
    counting. ``cost_*`` may be ``None`` when no price table is available, but
    tokens must never be lost.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str = Field(default=CURRENT_SCHEMA_VERSION)
    record_id: str = Field(min_length=1, description="Unique record id; never empty.")
    idempotency_key: str = Field(min_length=1, description="Producer-stable idempotency key; never empty.")
    org_id: str = Field(min_length=1, description="Owning org (inherited from RunEnvelope); never empty.")
    workspace_id: str | None = Field(default=None, description="Optional workspace within org_id.")
    run_id: str = Field(min_length=1, description="Associated run; never empty.")
    release_digest: str = Field(min_length=1, description="Release digest pinned on the run; never empty.")
    provider: str = Field(min_length=1, description="Model provider id; never empty.")
    model: str = Field(min_length=1, description="Model id; never empty.")
    attempt: int = Field(ge=0, description="Provider retry attempt index (0-based).")
    input_tokens: int = Field(ge=0, description="Input/prompt tokens from the model adapter.")
    output_tokens: int = Field(ge=0, description="Output/completion tokens from the model adapter.")
    cached_tokens: int = Field(ge=0, description="Cached prompt tokens from the model adapter.")
    cost_amount: str | None = Field(
        default=None,
        description="Decimal fixed-point cost string; None when no price table is available.",
    )
    cost_currency: str | None = Field(
        default=None,
        description="ISO 4217 currency code; None when cost_amount is None.",
    )
    started_at: datetime = Field(description="When the attempt started (UTC).")
    completed_at: datetime = Field(description="When the attempt completed (UTC).")
    status: UsageStatus = Field(description="success | failure | cancelled.")

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _normalize_utc(cls, value: object) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise TypeError("datetime must be an RFC 3339 datetime")
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return value.astimezone(UTC)


class AuditSink(Protocol):
    """Harness-facing audit emission Protocol (§10).

    Implementations live in the app layer. Class A strong-audit control-plane
    writes must succeed in the same transaction (or outbox); Class B runtime
    security events must reach a reliable local queue before the action returns.
    A sink that cannot persist must surface failure so the caller can fail
    closed — it must never silently drop an event.
    """

    def emit(self, event: AuditEvent) -> None:
        """Persist (or queue) an audit event. Never silently drop."""
        ...


class UsageRecorder(Protocol):
    """Harness-facing usage metering Protocol (§11).

    Implementations live in the app layer. Records are metering facts, not
    invoices; idempotency by ``idempotency_key`` prevents double counting on
    retry.
    """

    def record(self, record: UsageRecord) -> None:
        """Persist a usage record; idempotent by idempotency_key."""
        ...
