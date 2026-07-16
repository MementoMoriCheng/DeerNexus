"""RunEnvelope and integrity contracts.

``RunEnvelope`` is the trusted task envelope passed between Gateway, Scheduler,
Channel adapters and the executor (runtime-contracts.md §6). It carries the
pinned tenant context, resolved release ref, policy snapshot and an optional
integrity block for cross-trust-boundary transport.

Constraints enforced at the DTO level:

* the envelope is immutable and drops unknown fields (no secret smuggling);
* ``idempotency_key`` is non-empty — duplicate consumption must not create a
  second Run (§6);
* ``source`` is a closed set;
* ``integrity`` is optional: same-DB reads may omit it; cross-trust-boundary
  transport (message queue) must carry and verify it (§6).

The enforcement of ``run_id + org_id`` uniqueness, tenant/release consistency
and signature verification is the responsibility of the app adapter and is
covered by RunEnvelope contract tests (testing-strategy.md §7.3) in later PRs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deerflow.contracts.context import TenantContext
from deerflow.contracts.release import ReleaseRef
from deerflow.contracts.versioning import CURRENT_SCHEMA_VERSION

EnvelopeSource = Literal["api", "scheduler", "channel", "webhook", "internal"]
"""Closed set of envelope origins."""

IntegrityAlgorithm = Literal["hmac-sha256", "jwt"]
"""Closed set of integrity algorithms for cross-trust-boundary transport."""


class PolicySnapshotRef(BaseModel):
    """Reference to the policy version under which a Run was admitted.

    ``policy_version`` is persisted on the Run. It identifies the policy used
    for Run admission and ordinary skill/tool loading; high-risk actions are
    still re-evaluated in real time regardless of this snapshot (§7.3).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str = Field(default=CURRENT_SCHEMA_VERSION)
    policy_version: str = Field(min_length=1, description="Policy version used for admission.")
    evaluated_at: datetime = Field(description="When admission policy was evaluated (UTC).")
    expires_at: datetime | None = Field(default=None, description="Optional snapshot expiry.")

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


class EnvelopeIntegrity(BaseModel):
    """Integrity block for envelopes crossing a trust boundary.

    Same-DB reads (Gateway ↔ in-process executor) may leave ``integrity`` as
    ``None``. When an envelope crosses a message queue or process boundary, the
    producer must sign it and the consumer must verify before trusting it.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    algorithm: IntegrityAlgorithm = Field(description="Signing algorithm.")
    key_id: str = Field(min_length=1, description="Id of the signing key; never empty.")
    signature: str = Field(min_length=1, description="Signature value; never empty.")


class RunEnvelope(BaseModel):
    """Trusted task envelope consumed by the executor.

    The envelope is read from a trusted database or a signed message queue,
    never accepted directly from a client. ``tenant.org_id`` must agree with the
    persisted Run, Thread and ReleaseRef (verified by the adapter).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str = Field(default=CURRENT_SCHEMA_VERSION)
    run_id: str = Field(min_length=1, description="Run id; never empty.")
    thread_id: str = Field(min_length=1, description="Thread id; never empty.")
    tenant: TenantContext = Field(description="Trusted tenant context (rebuilt by the executor).")
    release_ref: ReleaseRef = Field(description="Pinned, immutable release reference.")
    policy_snapshot: PolicySnapshotRef = Field(description="Policy version snapshot at admission.")
    created_at: datetime = Field(description="When the envelope/run was created (UTC).")
    idempotency_key: str = Field(
        min_length=1,
        description="Idempotency key; duplicate consumption must not create a second Run.",
    )
    source: EnvelopeSource = Field(description="Origin of the run request.")
    source_ref: str | None = Field(
        default=None,
        description="External source reference (no secrets); e.g. webhook event id.",
    )
    integrity: EnvelopeIntegrity | None = Field(
        default=None,
        description="Integrity block; None for same-DB reads, required across trust boundaries.",
    )

    @field_validator("created_at", mode="before")
    @classmethod
    def _created_at_utc(cls, value: object) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise TypeError("created_at must be an RFC 3339 datetime")
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)
