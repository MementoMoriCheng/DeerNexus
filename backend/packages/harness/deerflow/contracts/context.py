"""TenantContext contract.

The immutable, tenant-scoped context consumed by the runtime kernel. Trusted
entry points (Gateway, Worker, Scheduler, Channel adapters) construct it after
authentication, organization resolution and workspace validation, then bind it
so downstream code operates inside a verified tenant scope.

This module freezes only the immutable DTO and its invariants
(runtime-contracts.md §5). The ContextVar lifecycle helpers
(``bind_tenant_context`` / ``get_tenant_context`` / ``require_tenant_context``
/ ``reset_tenant_context``) and the concurrency / cleanup tests (``TEN-001``
through ``TEN-009``) ship in PR-012; the DTO must exist first so the binding
machinery and the entry-point adapters have a stable type to target.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deerflow.contracts.identity import PrincipalRef
from deerflow.contracts.versioning import CURRENT_SCHEMA_VERSION

AuthMethod = Literal["oidc", "session", "api_key", "internal"]
"""How the principal was authenticated. Unknown values are rejected."""


class TenantContext(BaseModel):
    """Trusted tenant context for a request or trusted task.

    Invariants (runtime-contracts.md §5.1):

    * ``org_id`` is non-empty — the organization is the hard isolation
      boundary and never defaults to an implicit org;
    * the object is immutable (``frozen=True``);
    * it carries no secrets, tokens, session cookies or full OIDC claims —
      unknown fields are dropped so a leaked credential cannot ride along;
    * a client-supplied ``org_id`` is never the trusted source of truth; the
      trusted value is set by the entry point after membership resolution.

    ``issued_at`` is always timezone-aware and normalized to UTC.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str = Field(
        default=CURRENT_SCHEMA_VERSION,
        description="Contracts schema version.",
    )
    org_id: str = Field(
        min_length=1,
        description="Organization id (hard isolation boundary); never empty.",
    )
    workspace_id: str | None = Field(
        default=None,
        description="Optional workspace within org_id; must belong to org_id when set.",
    )
    principal: PrincipalRef = Field(
        description="Authenticated principal behind this context.",
    )
    auth_method: AuthMethod = Field(
        description="How the principal was authenticated.",
    )
    request_id: str = Field(
        min_length=1,
        description="Per-request correlation id; never empty.",
    )
    trace_id: str | None = Field(
        default=None,
        description="Optional distributed trace id.",
    )
    issued_at: datetime = Field(
        description="When the context was bound (UTC, RFC 3339, timezone-aware).",
    )

    @field_validator("issued_at", mode="before")
    @classmethod
    def _issued_at_utc(cls, value: object) -> datetime:
        """Require a timezone-aware timestamp and normalize it to UTC."""
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise TypeError("issued_at must be an RFC 3339 datetime")
        if value.tzinfo is None:
            raise ValueError("issued_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)
