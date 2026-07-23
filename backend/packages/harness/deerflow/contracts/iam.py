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


# ---------------------------------------------------------------------------
# OIDC group-mapping contracts (PR-036) — ADR-0003 §10
# ---------------------------------------------------------------------------
#
# Envelopes for the ``/api/v1/iam/oidc-group-mappings`` admin CRUD + the
# ``:preview`` dry-run endpoint. The 6-field config model (ADR §10) maps
# 1:1 to the create-request fields. ``mode`` defaults to ``additive``
# (the MVP default); ``authoritative`` is stored but the mapping service
# refuses to enact it (ADR §10 "authoritative 模式需单独启用").
#
# ADR §10 rule 3 (no system permissions) is enforced by the router at
# create/update time: it looks up ``target_role_id`` and rejects if the
# role carries any ``system:*`` permission. The contract layer cannot do
# that check (it has no DB access), so the validation is a router
# responsibility and ``target_role_id`` is an opaque string here.


class OidcGroupMappingCreateRequest(BaseModel):
    """Body of ``POST /api/v1/iam/oidc-group-mappings``.

    Maps 1:1 to the ADR §10 6-field config model. ``mode`` defaults to
    ``additive``; the router validates ``target_role_id`` references a
    real, non-system role (rule 3) before persisting.
    """

    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(min_length=1, max_length=500, description="OIDC issuer URL (must match the verified token ``iss`` claim).")
    group_claim: str = Field(min_length=1, max_length=120, description="Claim NAME carrying group membership (e.g. ``groups``).")
    group_value: str = Field(min_length=1, max_length=200, description="Group value within ``group_claim`` that this rule matches.")
    target_org_id: str = Field(min_length=1, max_length=36)
    target_role_id: str = Field(min_length=1, max_length=36, description="Existing role id; the router rejects a system-permission role (ADR §10 rule 3).")
    mode: str = Field(default="additive", description="``additive`` (default) or ``authoritative`` (stored; not enacted in MVP).")
    description: str | None = Field(default=None, max_length=2000)


class OidcGroupMappingUpdateRequest(BaseModel):
    """Body of ``PATCH /api/v1/iam/oidc-group-mappings/{id}``.

    ``issuer`` and ``target_org_id`` are deliberately absent (immutable —
    a rule's identity is fixed; retarget = delete + recreate for a clean
    audit trail). ``target_role_id`` IS patchable and the router
    re-validates rule 3 on update.
    """

    model_config = ConfigDict(extra="forbid")

    group_claim: str | None = Field(default=None, min_length=1, max_length=120)
    group_value: str | None = Field(default=None, min_length=1, max_length=200)
    target_role_id: str | None = Field(default=None, min_length=1, max_length=36)
    mode: str | None = Field(default=None, description="``additive`` or ``authoritative``.")
    description: str | None = Field(default=None, max_length=2000)


class OidcGroupMappingResponse(BaseModel):
    """Response envelope for OIDC group-mapping reads.

    Projected directly off :class:`~deerflow.persistence.iam.model.OidcGroupMappingRow`
    via ``from_attributes`` so the API surface and the ORM cannot drift.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    issuer: str
    group_claim: str
    group_value: str
    target_org_id: str
    target_role_id: str
    mode: str
    description: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class OidcMappingPreviewRequest(BaseModel):
    """Body of ``POST /api/v1/iam/oidc-group-mappings:preview`` (dry-run).

    The IdP-agnostic claim shape the operator wants to simulate: an
    ``issuer`` and the ``groups`` list the user presented. The router
    resolves the caller's own ``user_id`` + active-membership org, so no
    user_id is in the request — the preview always runs against the
    caller, never an arbitrary target (avoiding a "dry-run as
    reconnaissance" abuse vector).
    """

    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(min_length=1, max_length=500)
    groups: list[str] = Field(min_length=1, description="Non-empty group claim list to simulate.")


class _PreviewOutcome(BaseModel):
    """One mapping rule's disposition in a preview result."""

    group_value: str
    target_role_id: str
    target_org_id: str
    applied: bool
    reason: str = ""


class OidcMappingPreviewResponse(BaseModel):
    """Response envelope for the dry-run preview.

    A projection of :class:`~deerflow.tenancy.oidc_group_mapping.MappingResult`
    — ``planned``/``applied``/``skipped`` are all present so the operator
    sees the full disposition regardless of mode. In a dry-run ``applied``
    is always empty (nothing is written).
    """

    user_id: str
    issuer: str
    dry_run: bool
    planned: list[_PreviewOutcome] = Field(default_factory=list)
    applied: list[_PreviewOutcome] = Field(default_factory=list)
    skipped: list[_PreviewOutcome] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# OrgMembership contracts (PR-037) — ADR-0003 §7 + §11
# ---------------------------------------------------------------------------
#
# The membership suspend/activate endpoints are the revocation write path
# that §11's SLO measures: ``suspend`` commits the status change, then the
# router invalidates the principal's authz cache so the next request (and
# any in-flight SSE re-validation) sees the denial within the ≤60s bound.


class OrgMembershipResponse(BaseModel):
    """Response envelope for OrgMembership reads (PR-037).

    Projected directly off :class:`~deerflow.persistence.orgs.model.OrgMembershipRow`
    via ``from_attributes``. The ``(org_id, user_id)`` pair is the caller's
    active org + the target user.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    user_id: str
    status: str
    joined_at: datetime | None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "ApiKeyCreateRequest",
    "ApiKeyCreateResponse",
    "ApiKeyResponse",
    "OidcGroupMappingCreateRequest",
    "OidcGroupMappingResponse",
    "OidcGroupMappingUpdateRequest",
    "OidcMappingPreviewRequest",
    "OidcMappingPreviewResponse",
    "OrgMembershipResponse",
    "ServiceAccountCreateRequest",
    "ServiceAccountRoleBindingRequest",
    "ServiceAccountRoleBindingResponse",
    "ServiceAccountResponse",
    "ServiceAccountUpdateRequest",
]
