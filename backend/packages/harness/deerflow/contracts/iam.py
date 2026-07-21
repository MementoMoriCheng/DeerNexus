"""Request / response contracts for the IAM ServiceAccount + API Key APIs (PR-034 / PR-035).

Pydantic envelopes for ``app/gateway/routers/iam.py``. These mirror the
``ServiceAccountRow`` columns added by migration ``0008`` (ADR §9.1
traceability: owner / purpose / system / environment / expires_at)
plus the lifecycle endpoints' payloads (enable / disable / role
binding create) and the API Key endpoints (mint / list / revoke).

Kept in ``deerflow.contracts`` because the harness boundary
(``test_harness_boundary``) requires DTOs the app layer depends on to
live in contracts — the router imports these directly. The module
imports only Pydantic base types + ``datetime`` + stdlib typing, so it
carries no ORM / FastAPI / LangGraph dependency.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ServiceAccountCreateRequest(BaseModel):
    """Body of ``POST /api/v1/iam/service-accounts``.

    All optional fields beyond ``name`` map 1:1 to the ADR §9.1
    traceability columns added by migration ``0008``. ``expires_at`` is
    a review-by date (operator-negotiated checkpoint), NOT a credential
    expiry — the ServiceAccount itself does not auto-expire.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120, description="Unique within the Org.")
    description: str | None = Field(default=None)
    owner_user_id: str | None = Field(default=None, description="Accountability contact. NOT a grant source.")
    purpose: str | None = Field(default=None, max_length=256)
    system: str | None = Field(default=None, max_length=64)
    environment: str | None = Field(default=None, max_length=32)
    expires_at: datetime | None = Field(default=None, description="Review-by date (not a credential expiry).")


class ServiceAccountUpdateRequest(BaseModel):
    """Body of ``PATCH /api/v1/iam/service-accounts/{sa_id}``.

    All fields optional (PATCH semantics). ``status`` is deliberately
    absent — lifecycle transitions go through the dedicated
    ``:disable`` / ``:enable`` endpoints so the call site is explicit
    about which side of the state machine it is exercising.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    owner_user_id: str | None = None
    purpose: str | None = Field(default=None, max_length=256)
    system: str | None = Field(default=None, max_length=64)
    environment: str | None = Field(default=None, max_length=32)
    expires_at: datetime | None = None


class ServiceAccountResponse(BaseModel):
    """Response envelope for ServiceAccount reads.

    Fields are a 1:1 projection of ``ServiceAccountRow`` so the API
    surface and the ORM cannot drift silently. ``model_config =
    ConfigDict(from_attributes=True)`` lets the router construct the
    response directly from the row via ``ServiceAccountResponse.model_validate(row)``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    name: str
    description: str | None
    status: str
    owner_user_id: str | None
    purpose: str | None
    system: str | None
    environment: str | None
    expires_at: datetime | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None


class ServiceAccountRoleBindingRequest(BaseModel):
    """Body of ``POST /api/v1/iam/service-accounts/{sa_id}/role-bindings``."""

    model_config = ConfigDict(extra="forbid")

    role_id: str = Field(min_length=1)
    expires_at: datetime | None = Field(default=None)


class ServiceAccountRoleBindingResponse(BaseModel):
    """Response envelope for ServiceAccount role-binding reads.

    Like :class:`ServiceAccountResponse`, projected directly off
    ``RoleBindingRow`` via ``from_attributes``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    principal_id: str
    role_id: str
    expires_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# API Key contracts (PR-035)
# ---------------------------------------------------------------------------
#
# ADR §9.2 governs the Key rules. Two response shapes are mandated by
# the "明文只展示一次" rule (§9.2 line 293):
#
# * :class:`ApiKeyCreateResponse` (returned ONLY by POST mint) carries
#   ``plaintext_key`` exactly once. The server never persists the
#   plaintext — only ``key_hash`` lands in the DB.
# * :class:`ApiKeyResponse` (returned by GET list) deliberately omits
#   both ``plaintext_key`` and ``key_hash`` — the read path must never
#   surface either. The two classes are intentionally NOT in a
#   subclass relationship on the read side (the create response
#   subclasses the read response because the create response is a
#   superset; reads never see the superset).
#
# ADR §9.2 line 300 also mandates that ``scopes`` be non-empty at
# creation; enforced here via ``min_length=1`` so a malformed client
# request fails at the pydantic boundary, before any DB write.


class ApiKeyCreateRequest(BaseModel):
    """Body of ``POST /api/v1/iam/service-accounts/{sa_id}/api-keys``."""

    model_config = ConfigDict(extra="forbid")

    scopes: list[str] = Field(
        min_length=1,
        description=("Non-empty (ADR §9.2 line 300). Each entry MUST be a known Permission value and not carry the system: prefix — the router validates via ``validate_role_permissions``."),
    )
    expires_at: datetime = Field(
        description=("Required (ADR §9.2 line 296 mandates ≤90 day default; the router clamps to that bound)."),
    )
    description: str | None = Field(
        default=None,
        description=("Optional human note carried in the audit payload only; NOT persisted on ApiKeyRow (no column). Reserved for future use."),
    )


class ApiKeyResponse(BaseModel):
    """Response envelope for API Key reads. NEVER carries plaintext or hash."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    service_account_id: str
    key_prefix: str
    scopes: list[str]
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime
    last_used_at: datetime | None


class ApiKeyCreateResponse(ApiKeyResponse):
    """Response for the mint endpoint. Carries ``plaintext_key`` exactly once.

    Subclasses :class:`ApiKeyResponse` (superset — adds one field). The
    router returns this ONLY from the POST endpoint; the GET list
    endpoint uses :class:`ApiKeyResponse` directly so the plaintext can
    never leak through a read path.
    """

    plaintext_key: str = Field(
        description=("Full API key. Returned exactly once (ADR §9.2 line 293). NEVER persisted, NEVER logged, NEVER出现在 audit payload."),
    )


__all__ = [
    "ApiKeyCreateRequest",
    "ApiKeyCreateResponse",
    "ApiKeyResponse",
    "ServiceAccountCreateRequest",
    "ServiceAccountRoleBindingRequest",
    "ServiceAccountRoleBindingResponse",
    "ServiceAccountResponse",
    "ServiceAccountUpdateRequest",
]
