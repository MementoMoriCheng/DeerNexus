"""Unit tests for the PR-010 deerflow.contracts base package.

Covers PrincipalRef (CONTRACT-010-IDENT), TenantContext invariants
(CONTRACT-010-TENANT), the ContractError envelope / ErrorCode registry
(CONTRACT-010-ERROR), fixture conformance (CONTRACT-010-FIXTURE), immutability
(CONTRACT-010-IMMUTABLE) and the no-secret surface rule
(CONTRACT-010-NO-SECRET).

These are pure contract tests — no app / ORM / FastAPI dependency is imported.
The boundary between harness contracts and the app layer is enforced separately
in ``tests/test_harness_boundary.py``.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from deerflow.contracts import (
    CURRENT_SCHEMA_VERSION,
    ContractError,
    ErrorCode,
    PrincipalRef,
    TenantContext,
    is_retryable_code,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "contracts"


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _user_principal(
    *,
    id: str = "4a3f2c1e-9b8d-4a7e-b6c5-1a2b3c4d5e6f",
    user_id: str = "4a3f2c1e-9b8d-4a7e-b6c5-1a2b3c4d5e6f",
    display_name: str | None = "Ada Lovelace",
) -> PrincipalRef:
    return PrincipalRef(type="user", id=id, user_id=user_id, display_name=display_name)


def _tenant_context(**overrides) -> TenantContext:
    base: dict = {
        "org_id": "9f1c2b3a-4d5e-4789-abcd-ef0123456789",
        "principal": _user_principal(),
        "auth_method": "oidc",
        "request_id": "7b8e9f0a-1234-5678-9abc-def012345678",
        "issued_at": "2026-07-15T10:00:00Z",
    }
    base.update(overrides)
    return TenantContext(**base)


# ===========================================================================
# PrincipalRef — CONTRACT-010-IDENT
# ===========================================================================


class TestPrincipalRef:
    def test_user_principal_accepts_user_id(self):
        p = _user_principal()
        assert p.type == "user"
        assert p.id == p.user_id

    @pytest.mark.parametrize("ptype", ["service_account", "system"])
    def test_non_user_principal_rejects_user_id(self, ptype):
        with pytest.raises(ValidationError) as exc:
            PrincipalRef(type=ptype, id="x", user_id="u-1")
        assert "user_id" in str(exc.value)

    def test_id_must_be_non_empty(self):
        with pytest.raises(ValidationError):
            PrincipalRef(type="user", id="", user_id="u")

    def test_unknown_type_is_rejected(self):
        # Literal type rejects unknown principal categories (fail-closed,
        # never silently remap an unexpected identity type).
        with pytest.raises(ValidationError):
            PrincipalRef(type="bot", id="x")  # type: ignore[arg-type]

    def test_extra_fields_dropped(self):
        # extra="ignore": a leaked credential cannot ride along on the DTO.
        p = PrincipalRef(
            type="user",
            id="u-1",
            user_id="u-1",
            access_token="secret",  # type: ignore[call-arg]
        )
        assert not hasattr(p, "access_token")


# ===========================================================================
# TenantContext — CONTRACT-010-TENANT
# ===========================================================================


class TestTenantContext:
    def test_defaults_to_current_schema_version(self):
        t = _tenant_context()
        assert t.schema_version == CURRENT_SCHEMA_VERSION
        assert t.schema_version == "v1alpha1"

    def test_org_id_must_be_non_empty(self):
        with pytest.raises(ValidationError):
            _tenant_context(org_id="")

    def test_request_id_must_be_non_empty(self):
        with pytest.raises(ValidationError):
            _tenant_context(request_id="")

    def test_principal_is_required(self):
        with pytest.raises(ValidationError):
            TenantContext(
                org_id="org-1",
                auth_method="oidc",
                request_id="req-1",
                issued_at="2026-07-15T10:00:00Z",
            )

    @pytest.mark.parametrize("auth_method", ["oidc", "session", "api_key", "internal"])
    def test_known_auth_methods_accepted(self, auth_method):
        t = _tenant_context(auth_method=auth_method)
        assert t.auth_method == auth_method

    def test_unknown_auth_method_rejected(self):
        with pytest.raises(ValidationError):
            _tenant_context(auth_method="mtls")  # type: ignore[arg-type]

    def test_issued_at_normalized_to_utc(self):
        # +02:00 offset is normalized to UTC Z.
        t = _tenant_context(issued_at="2026-07-15T12:00:00+02:00")
        assert t.issued_at == datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
        # serialized form is UTC RFC 3339 with 'Z'
        assert t.issued_at.isoformat().endswith("+00:00")

    def test_naive_issued_at_rejected(self):
        # a timezone-unaware timestamp could mask a local-clock bug; reject it.
        with pytest.raises(ValidationError):
            _tenant_context(issued_at=datetime(2026, 7, 15, 10, 0))

    def test_workspace_id_optional(self):
        t = _tenant_context(workspace_id=None)
        assert t.workspace_id is None
        t2 = _tenant_context(workspace_id="ws-1")
        assert t2.workspace_id == "ws-1"

    def test_extra_fields_dropped(self):
        # a client-supplied org_id / secret must never become a trusted field;
        # unknown fields are dropped, and org_id cannot be overwritten via body
        # because the trusted value is what the entry point passes.
        t = TenantContext(
            org_id="trusted-org",
            principal=_user_principal(),
            auth_method="oidc",
            request_id="req-1",
            issued_at="2026-07-15T10:00:00Z",
            client_org_id="attacker-org",  # type: ignore[call-arg]
            session_cookie="secret",  # type: ignore[call-arg]
        )
        assert not hasattr(t, "client_org_id")
        assert not hasattr(t, "session_cookie")
        assert t.org_id == "trusted-org"


# ===========================================================================
# Immutability — CONTRACT-010-IMMUTABLE
# ===========================================================================


class TestImmutability:
    @pytest.mark.parametrize(
        "builder",
        [_user_principal, _tenant_context],
        ids=["PrincipalRef", "TenantContext"],
    )
    def test_model_is_frozen(self, builder):
        obj = builder()
        with pytest.raises(ValidationError):
            setattr(obj, "id" if isinstance(obj, PrincipalRef) else "org_id", "changed")

    def test_nested_principal_is_frozen(self):
        t = _tenant_context()
        with pytest.raises(ValidationError):
            t.principal.id = "changed"  # type: ignore[misc]

    def test_model_copy_with_changes_allowed(self):
        # frozen models still support explicit, validated replacement.
        p = _user_principal()
        p2 = p.model_copy(update={"display_name": "Grace Hopper"})
        assert p2.display_name == "Grace Hopper"
        assert p.display_name == "Ada Lovelace"


# ===========================================================================
# ContractError + ErrorCode — CONTRACT-010-ERROR
# ===========================================================================


# Codes that the §12 table marks retryable.
_RETRYABLE = {
    ErrorCode.POLICY_UNAVAILABLE,
    ErrorCode.RELEASE_CONFLICT,
    ErrorCode.RUN_CONFLICT,
    ErrorCode.AUDIT_UNAVAILABLE,
    ErrorCode.RATE_LIMITED,
}
# Known non-retryable codes (security-relevant: must not become retryable).
_NON_RETRYABLE = {
    ErrorCode.TENANT_CONTEXT_MISSING,
    ErrorCode.TENANT_MISMATCH,
    ErrorCode.AUTHENTICATION_INVALID,
    ErrorCode.PRINCIPAL_DISABLED,
    ErrorCode.ORG_SUSPENDED,
    ErrorCode.ORG_DELETING,
    ErrorCode.PERMISSION_DENIED,
    ErrorCode.POLICY_DENIED,
    ErrorCode.APPROVAL_REQUIRED,
    ErrorCode.RELEASE_NOT_FOUND,
    ErrorCode.RELEASE_NOT_PUBLISHED,
    ErrorCode.RELEASE_REVOKED,
    ErrorCode.RELEASE_UNPINNED,
    ErrorCode.RELEASE_TENANT_MISMATCH,
    ErrorCode.IDEMPOTENCY_CONFLICT,
    ErrorCode.VALIDATION_ERROR,
}


class TestErrorCodeRegistry:
    def test_registry_matches_mvp_set(self):
        # Guard against silent drift: every code in runtime-contracts.md §12
        # must be present exactly once, with its canonical string value.
        assert {c.value for c in ErrorCode} == _RETRYABLE | _NON_RETRYABLE
        assert len(ErrorCode) == 21

    @pytest.mark.parametrize("code", list(_RETRYABLE))
    def test_retryable_codes(self, code):
        assert is_retryable_code(code) is True
        env = ContractError.from_code(code, request_id="req-1")
        assert env.retryable is True

    @pytest.mark.parametrize("code", list(_NON_RETRYABLE))
    def test_non_retryable_codes(self, code):
        # security-relevant: tenant / auth / release-safety codes must not be
        # auto-retried, since a retry can amplify a denial or mask a failure.
        assert is_retryable_code(code) is False
        env = ContractError.from_code(code, request_id="req-1")
        assert env.retryable is False

    def test_tenant_context_missing_is_non_retryable(self):
        # Specifically called out by TEN-007 / §12: a missing trusted context
        # must never silently fall back or auto-retry.
        env = ContractError.from_code(ErrorCode.TENANT_CONTEXT_MISSING, request_id="req-1")
        assert env.retryable is False

    def test_release_unpinned_is_non_retryable(self):
        # prod gating: a 409 release_unpinned is terminal, not retryable.
        env = ContractError.from_code(ErrorCode.RELEASE_UNPINNED, request_id="req-1")
        assert env.retryable is False


class TestContractErrorEnvelope:
    def test_from_code_defaults_message_to_code(self):
        env = ContractError.from_code(ErrorCode.TENANT_MISMATCH, request_id="req-1")
        assert env.code == "tenant_mismatch"
        assert env.message == "tenant_mismatch"
        assert env.details == {}

    def test_from_code_accepts_message_and_details(self):
        env = ContractError.from_code(
            ErrorCode.VALIDATION_ERROR,
            request_id="req-1",
            message="org_id is required",
            details={"field": "org_id"},
        )
        assert env.message == "org_id is required"
        assert env.details == {"field": "org_id"}

    def test_request_id_required(self):
        with pytest.raises(ValidationError):
            ContractError(  # type: ignore[call-arg]
                code="tenant_context_missing",
                message="x",
                retryable=False,
            )

    def test_envelope_is_frozen(self):
        env = ContractError.from_code(ErrorCode.RATE_LIMITED, request_id="req-1")
        with pytest.raises(ValidationError):
            env.code = "permission_denied"  # type: ignore[misc]


# ===========================================================================
# Serialization round-trips — CONTRACT-010-FIXTURE (producer/consumer parity)
# ===========================================================================


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


class TestCanonicalFixtures:
    @pytest.mark.parametrize(
        ("model", "fixture_name"),
        [
            (PrincipalRef, "principal_ref.json"),
            (TenantContext, "tenant_context.json"),
            (ContractError, "contract_error.json"),
        ],
    )
    def test_fixture_loads_into_model(self, model: type[BaseModel], fixture_name: str):
        data = _load_fixture(fixture_name)
        obj = model.model_validate(data)
        assert obj is not None

    @pytest.mark.parametrize(
        ("model", "fixture_name"),
        [
            (PrincipalRef, "principal_ref.json"),
            (TenantContext, "tenant_context.json"),
            (ContractError, "contract_error.json"),
        ],
    )
    def test_fixture_round_trips_stably(self, model: type[BaseModel], fixture_name: str):
        # Round-trip stability is what keeps producers and consumers in sync:
        # a fixture must deserialize, then re-serialize to the same field set.
        data = _load_fixture(fixture_name)
        obj = model.model_validate(data)
        round_tripped = model.model_validate(obj.model_dump(mode="json"))
        assert round_tripped == obj

    def test_tenant_context_fixture_uses_v1alpha1(self):
        assert _load_fixture("tenant_context.json")["schema_version"] == "v1alpha1"

    def test_tenant_context_fixture_issued_at_is_utc(self):
        raw = _load_fixture("tenant_context.json")["issued_at"]
        assert raw.endswith("Z"), "fixture issued_at must be UTC RFC 3339"
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None


# ===========================================================================
# Unknown-field compatibility — CONTRACT-010-COMPAT (§13.2)
# ===========================================================================


class TestForwardCompatibility:
    @pytest.mark.parametrize(
        ("model", "payload"),
        [
            (PrincipalRef, {"type": "user", "id": "u-1", "user_id": "u-1", "future_field": "x"}),
            (
                TenantContext,
                {
                    "org_id": "org-1",
                    "principal": {"type": "user", "id": "u-1", "user_id": "u-1"},
                    "auth_method": "oidc",
                    "request_id": "req-1",
                    "issued_at": "2026-07-15T10:00:00Z",
                    "future_field": "x",
                },
            ),
        ],
    )
    def test_unknown_optional_fields_are_ignored(self, model, payload):
        # Adding an optional field in a future schema version is a compatible
        # change: old consumers must ignore it, not crash.
        deep = copy.deepcopy(payload)
        obj = model.model_validate(deep)
        assert not hasattr(obj, "future_field")

    def test_missing_required_field_fails(self):
        # Removing a required field must fail loudly (not silently default).
        with pytest.raises(ValidationError):
            PrincipalRef.model_validate({"type": "user"})  # missing id
