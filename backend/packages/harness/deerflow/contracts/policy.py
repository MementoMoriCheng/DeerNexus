"""Policy contracts.

Freezes the authorization request/response model and the harness-facing
``PolicyEvaluator`` Protocol (runtime-contracts.md §7). The harness only knows
this Protocol; concrete evaluation may be an in-process app adapter, a cached
snapshot or a remote service.

Design rules:

* ``ResourceRef.org_id`` is non-empty — a resource is always scoped to a tenant;
* ``PolicyRequest.context`` is an allow-listed policy bag (tool name, target
  domain, model id, data classification); it must never carry secrets, full
  prompts or file contents (§7.1);
* ``risk_class`` is a closed set; the caller may never downgrade it (§7.3);
* high/critical evaluations must fail closed on unavailability — that behaviour
  is implemented by the evaluator adapter and asserted in later integration
  tests; this contract only freezes the field surface;
* unknown ``PolicyObligation.type`` values must be safely rejected, never
  silently ignored (§7.2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deerflow.contracts.context import TenantContext
from deerflow.contracts.versioning import CURRENT_SCHEMA_VERSION

RiskClass = Literal["low", "medium", "high", "critical"]
"""Closed risk taxonomy. The caller may never downgrade risk_class (§7.3)."""

Decision = Literal["allow", "deny", "require_approval"]
"""Closed decision set. PolicyEvaluator must never return an empty/unknown decision."""

ObligationType = Literal["audit", "redact", "limit", "approval_stub"]
"""Closed obligation taxonomy for MVP (§7.2). Unknown types must be rejected."""


class ResourceRef(BaseModel):
    """Tenant-scoped resource reference used by policy and audit events.

    ``org_id`` is required and non-empty because every resource belongs to an
    organization (the hard isolation boundary). ``attributes`` is an allow-listed
    bag of non-sensitive policy hints (never secrets or full content).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    type: str = Field(
        min_length=1,
        description="Resource type identifier (e.g. 'thread', 'run', 'artifact').",
    )
    id: str | None = Field(
        default=None,
        description="Resource id when applicable; None for type-level checks.",
    )
    org_id: str = Field(
        min_length=1,
        description="Owning organization; never empty.",
    )
    workspace_id: str | None = Field(
        default=None,
        description="Optional workspace within org_id.",
    )
    attributes: dict = Field(
        default_factory=dict,
        description="Allow-listed, non-sensitive policy hints; never secrets/prompts/content.",
    )


class PolicyRequest(BaseModel):
    """A single authorization question posed to the policy evaluator."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str = Field(default=CURRENT_SCHEMA_VERSION)
    request_id: str = Field(min_length=1, description="Correlation id; never empty.")
    tenant: TenantContext = Field(description="Trusted tenant context for this request.")
    run_id: str | None = Field(default=None, description="Associated run, if any.")
    action: str = Field(min_length=1, description="Action verb (e.g. 'runtime:run:create').")
    resource: ResourceRef = Field(description="Target resource, always tenant-scoped.")
    risk_class: RiskClass = Field(description="Risk class; caller may not downgrade.")
    context: dict = Field(
        default_factory=dict,
        description="Allow-listed policy context (tool name, domain, model id, classification); no secrets.",
    )


class PolicyObligation(BaseModel):
    """A side effect the runtime must honour when a decision is 'allow'.

    Obligation ``parameters`` follow a per-type allow-list schema (§7.2).
    Unknown obligation types must be safely rejected by the consumer, not
    ignored — an ignored ``limit`` or ``redact`` obligation is a safety hole.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    type: ObligationType = Field(description="Obligation category; unknown types must be rejected.")
    parameters: dict = Field(
        default_factory=dict,
        description="Per-type allow-listed parameters; no secrets.",
    )


class PolicyDecision(BaseModel):
    """The evaluator's answer to a PolicyRequest.

    The evaluator must never return an empty decision. ``deny`` and
    ``require_approval`` must be honoured as terminal/safe-interrupt by the
    caller (§7.4).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str = Field(default=CURRENT_SCHEMA_VERSION)
    decision: Decision = Field(description="allow | deny | require_approval; never empty.")
    reason_code: str = Field(min_length=1, description="Stable machine reason code.")
    reason: str = Field(default="", description="Human-readable reason; no secrets or policy internals.")
    rule_id: str | None = Field(default=None, description="Matching rule id, if known.")
    policy_version: str = Field(min_length=1, description="Policy version that produced this decision.")
    evaluated_at: datetime = Field(description="When the decision was made (UTC).")
    expires_at: datetime | None = Field(
        default=None,
        description="Optional expiry; high-risk real-time decisions may set a short TTL.",
    )
    obligations: list[PolicyObligation] = Field(
        default_factory=list,
        description="Obligations the caller must honour when acting on 'allow'.",
    )

    @field_validator("evaluated_at", "expires_at", mode="before")
    @classmethod
    def _normalize_utc(cls, value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise TypeError("datetime must be an RFC 3339 datetime")
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return value.astimezone(UTC)


class PolicyEvaluator(Protocol):
    """Harness-facing authorization Protocol (§7.5).

    Implementations live in the app layer (in-process adapter, cached snapshot,
    or remote service). The harness depends only on this Protocol. A returning
    ``PolicyDecision`` is always non-empty; evaluation failure surfaces as a
    ``ContractError`` with an appropriate code (e.g. ``policy_unavailable``).
    """

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        """Evaluate a single authorization request and return a non-empty decision."""
        ...
