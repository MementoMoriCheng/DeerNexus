"""Release contracts.

Freezes the immutable agent-artifact execution reference (``ReleaseRef``) and
the harness-facing ``ReleaseResolver`` Protocol (runtime-contracts.md §8,
ADR-0004 §6).

Key rules:

* ``digest`` is the immutable content identity, initially ``sha256:<hex>``
  (ADR-0004 §3.2); ``version`` is human-readable SemVer for display/sort only;
* ``channel`` is a closed set: ``dev`` | ``staging`` | ``prod``;
* prod may only resolve a ``published``, non-revoked version (enforced by the
  resolver adapter, not the DTO);
* a Run pins the full ReleaseRef at creation; the execution stage never
  re-reads the channel (§8.1);
* cross-Org release references are forbidden (§8.1) — ``org_id`` is non-empty
  and resolution happens within the tenant scope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deerflow.contracts.context import TenantContext
from deerflow.contracts.versioning import CURRENT_SCHEMA_VERSION

ReleaseChannel = Literal["dev", "staging", "prod"]
"""Closed channel set. Environment↔channel mapping is operator config, not client choice."""


class ReleaseRef(BaseModel):
    """Immutable execution reference to a resolved agent artifact version.

    Pinned into the Run at creation time. Once pinned, the Run does not drift
    on channel promote/rollback, filesystem changes, or catalog sync (§8.1).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str = Field(default=CURRENT_SCHEMA_VERSION)
    org_id: str = Field(min_length=1, description="Owning org; never empty; cross-Org refs forbidden.")
    workspace_id: str | None = Field(default=None, description="Optional workspace within org_id.")
    package_id: str = Field(min_length=1, description="Agent package id (stable logical identity).")
    agent_name: str = Field(min_length=1, description="Human-readable agent/package name.")
    version: str = Field(min_length=1, description="SemVer display string; execution identity is digest.")
    digest: str = Field(
        min_length=1,
        description="Immutable content digest, initially 'sha256:<hex>'.",
    )
    channel: ReleaseChannel = Field(description="Channel resolved at pin time; not re-read during execution.")
    resolved_at: datetime = Field(description="When the ref was resolved (UTC).")

    @field_validator("resolved_at", mode="before")
    @classmethod
    def _resolved_at_utc(cls, value: object) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise TypeError("resolved_at must be an RFC 3339 datetime")
        if value.tzinfo is None:
            raise ValueError("resolved_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)


class ReleaseResolver(Protocol):
    """Harness-facing release resolution Protocol (§8.2).

    The resolver lives in the app adapter. The harness only consumes the
    returned ``ReleaseRef``. Resolution failure raises a ``ContractError``
    (e.g. ``release_not_found``, ``release_not_published``, ``release_revoked``,
    ``release_tenant_mismatch``).
    """

    def resolve(
        self,
        tenant: TenantContext,
        agent_name: str,
        channel: str,
    ) -> ReleaseRef:
        """Resolve the current version of ``agent_name`` on ``channel`` for ``tenant``."""
        ...
