"""Unit tests for the PR-011 Policy / Release / Event contracts.

Covers the second wave of runtime contracts shipped after PR-010:
Policy (CONTRACT-011-POLICY), Release (CONTRACT-011-RELEASE),
RunEnvelope (CONTRACT-011-ENVELOPE), ApprovalTicket (CONTRACT-011-APPROVAL),
AuditEvent (CONTRACT-011-AUDIT), UsageRecord (CONTRACT-011-USAGE), the sink /
evaluator Protocols (CONTRACT-011-PROTO), fixture conformance
(CONTRACT-011-FIXTURE), immutability (CONTRACT-011-IMMUTABLE) and the no-secret
surface (CONTRACT-011-NO-SECRET).

These are pure contract tests — no app / ORM / FastAPI dependency is imported.
The dependency boundary is enforced in ``tests/test_harness_boundary.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from deerflow.contracts import (
    ApprovalTicket,
    AuditEvent,
    EnvelopeIntegrity,
    PolicyDecision,
    PolicyObligation,
    PolicyRequest,
    PolicySnapshotRef,
    PrincipalRef,
    ReleaseRef,
    ResourceRef,
    RunEnvelope,
    TenantContext,
    UsageRecord,
)
from deerflow.contracts.events import _FORBIDDEN_PAYLOAD_KEYS

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "contracts"

ORG_A = "9f1c2b3a-4d5e-4789-abcd-ef0123456789"
ORG_B = "11111111-2222-3333-4444-555555555555"
REQ_ID = "7b8e9f0a-1234-5678-9abc-def012345678"
TS = "2026-07-16T10:00:00Z"


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _principal(org: str = ORG_A) -> PrincipalRef:
    return PrincipalRef(type="user", id="u-1", user_id="u-1", display_name="Ada")


def _tenant(org: str = ORG_A) -> TenantContext:
    return TenantContext(
        org_id=org,
        principal=_principal(org),
        auth_method="oidc",
        request_id=REQ_ID,
        issued_at=TS,
    )


def _resource(org: str = ORG_A) -> ResourceRef:
    return ResourceRef(type="run", id="run-1", org_id=org)


def _policy_request(**overrides) -> PolicyRequest:
    base: dict = {
        "request_id": REQ_ID,
        "tenant": _tenant(),
        "action": "runtime:run:create",
        "resource": _resource(),
        "risk_class": "high",
    }
    base.update(overrides)
    return PolicyRequest(**base)


def _policy_decision(**overrides) -> PolicyDecision:
    base: dict = {
        "decision": "allow",
        "reason_code": "rule_match",
        "policy_version": "2026-07-15-01",
        "evaluated_at": TS,
    }
    base.update(overrides)
    return PolicyDecision(**base)


def _release_ref(org: str = ORG_A, channel: str = "prod") -> ReleaseRef:
    return ReleaseRef(
        org_id=org,
        package_id="pkg-1",
        agent_name="demo",
        version="1.2.0",
        digest="sha256:abcdef",
        channel=channel,
        resolved_at=TS,
    )


def _policy_snapshot() -> PolicySnapshotRef:
    return PolicySnapshotRef(
        policy_version="2026-07-15-01",
        evaluated_at=TS,
    )


def _run_envelope(**overrides) -> RunEnvelope:
    base: dict = {
        "run_id": "run-1",
        "thread_id": "th-1",
        "tenant": _tenant(),
        "release_ref": _release_ref(),
        "policy_snapshot": _policy_snapshot(),
        "created_at": TS,
        "idempotency_key": "idem-1",
        "source": "api",
    }
    base.update(overrides)
    return RunEnvelope(**base)


def _audit_event(**overrides) -> AuditEvent:
    base: dict = {
        "event_id": "evt-1",
        "idempotency_key": "ik-1",
        "org_id": ORG_A,
        "actor": _principal(),
        "action": "release.agent.published",
        "resource": _resource(),
        "outcome": "success",
        "request_id": REQ_ID,
        "occurred_at": TS,
    }
    base.update(overrides)
    return AuditEvent(**base)


def _usage_record(**overrides) -> UsageRecord:
    base: dict = {
        "record_id": "rec-1",
        "idempotency_key": "ik-u-1",
        "org_id": ORG_A,
        "run_id": "run-1",
        "release_digest": "sha256:abcdef",
        "provider": "openai",
        "model": "gpt-4o",
        "attempt": 0,
        "input_tokens": 100,
        "output_tokens": 50,
        "cached_tokens": 0,
        "started_at": TS,
        "completed_at": "2026-07-16T10:00:01Z",
        "status": "success",
    }
    base.update(overrides)
    return UsageRecord(**base)


def _approval_ticket(**overrides) -> ApprovalTicket:
    base: dict = {
        "ticket_id": "tkt-1",
        "org_id": ORG_A,
        "run_id": "run-1",
        "action": "tool:network:fetch",
        "risk_class": "high",
        "status": "pending",
        "resume_token_ref": "ref-1",
        "created_at": TS,
        "expires_at": "2026-07-16T11:00:00Z",
    }
    base.update(overrides)
    return ApprovalTicket(**base)


# ===========================================================================
# Policy — CONTRACT-011-POLICY
# ===========================================================================


class TestResourceRef:
    def test_org_id_required_non_empty(self):
        with pytest.raises(ValidationError):
            ResourceRef(type="run", id="r", org_id="")

    def test_type_required(self):
        with pytest.raises(ValidationError):
            ResourceRef(org_id=ORG_A)  # type: ignore[call-arg]

    def test_id_optional(self):
        r = ResourceRef(type="run", org_id=ORG_A)
        assert r.id is None

    def test_cross_org_resource_carries_its_own_org(self):
        # ResourceRef is the authority for a resource's owning org; it is not
        # inherited from TenantContext so a cross-Org mismatch is detectable.
        r = ResourceRef(type="run", id="r", org_id=ORG_B)
        assert r.org_id == ORG_B


class TestPolicyRequest:
    def test_known_risk_classes_accepted(self):
        for rc in ("low", "medium", "high", "critical"):
            assert _policy_request(risk_class=rc).risk_class == rc

    def test_unknown_risk_class_rejected(self):
        with pytest.raises(ValidationError):
            _policy_request(risk_class="trivial")  # type: ignore[arg-type]

    def test_action_required(self):
        with pytest.raises(ValidationError):
            _policy_request(action="")

    def test_context_is_allow_list_bag(self):
        pr = _policy_request(context={"tool_name": "bash", "target_domain": "example.com"})
        assert pr.context["tool_name"] == "bash"

    def test_extra_fields_dropped(self):
        pr = PolicyRequest(
            request_id=REQ_ID,
            tenant=_tenant(),
            action="x",
            resource=_resource(),
            risk_class="low",
            secret="leak",  # type: ignore[call-arg]
        )
        assert not hasattr(pr, "secret")


class TestPolicyDecision:
    @pytest.mark.parametrize("decision", ["allow", "deny", "require_approval"])
    def test_known_decisions_accepted(self, decision):
        assert _policy_decision(decision=decision).decision == decision

    def test_unknown_decision_rejected(self):
        with pytest.raises(ValidationError):
            _policy_decision(decision="maybe")  # type: ignore[arg-type]

    def test_policy_version_required(self):
        with pytest.raises(ValidationError):
            _policy_decision(policy_version="")

    def test_evaluated_at_normalized_to_utc(self):
        d = _policy_decision(evaluated_at="2026-07-16T12:00:00+02:00")
        assert d.evaluated_at == datetime(2026, 7, 16, 10, 0, tzinfo=UTC)

    def test_naive_evaluated_at_rejected(self):
        with pytest.raises(ValidationError):
            _policy_decision(evaluated_at=datetime(2026, 7, 16, 10, 0))

    def test_obligations_default_empty(self):
        assert _policy_decision().obligations == []


class TestPolicyObligation:
    @pytest.mark.parametrize("otype", ["audit", "redact", "limit", "approval_stub"])
    def test_known_obligation_types_accepted(self, otype):
        assert PolicyObligation(type=otype).type == otype

    def test_unknown_obligation_type_rejected(self):
        # Unknown obligations must be rejected (not silently ignored), because
        # an ignored 'limit' or 'redact' obligation is a safety hole (§7.2).
        with pytest.raises(ValidationError):
            PolicyObligation(type="delete_everything")  # type: ignore[arg-type]


# ===========================================================================
# Release — CONTRACT-011-RELEASE
# ===========================================================================


class TestReleaseRef:
    @pytest.mark.parametrize("channel", ["dev", "staging", "prod"])
    def test_known_channels_accepted(self, channel):
        assert _release_ref(channel=channel).channel == channel

    def test_unknown_channel_rejected(self):
        with pytest.raises(ValidationError):
            _release_ref(channel="canary")  # type: ignore[arg-type]

    def test_digest_required(self):
        # Construct fresh so the validator runs (model_copy bypasses validation).
        with pytest.raises(ValidationError):
            ReleaseRef(
                org_id=ORG_A,
                package_id="p",
                agent_name="a",
                version="1.0.0",
                digest="",
                channel="prod",
                resolved_at=TS,
            )

    def test_org_id_required(self):
        with pytest.raises(ValidationError):
            ReleaseRef(
                org_id="",
                package_id="p",
                agent_name="a",
                version="1.0.0",
                digest="sha256:x",
                channel="prod",
                resolved_at=TS,
            )

    def test_resolved_at_utc_normalized(self):
        # Construct fresh so the field validator runs and normalizes the offset.
        r = ReleaseRef(
            org_id=ORG_A,
            package_id="pkg-1",
            agent_name="demo",
            version="1.2.0",
            digest="sha256:abcdef",
            channel="prod",
            resolved_at="2026-07-16T12:00:00+02:00",
        )
        assert r.resolved_at == datetime(2026, 7, 16, 10, 0, tzinfo=UTC)


# ===========================================================================
# RunEnvelope — CONTRACT-011-ENVELOPE
# ===========================================================================


class TestRunEnvelope:
    @pytest.mark.parametrize("source", ["api", "scheduler", "channel", "webhook", "internal"])
    def test_known_sources_accepted(self, source):
        assert _run_envelope(source=source).source == source

    def test_unknown_source_rejected(self):
        with pytest.raises(ValidationError):
            _run_envelope(source="cli")  # type: ignore[arg-type]

    def test_idempotency_key_required(self):
        with pytest.raises(ValidationError):
            _run_envelope(idempotency_key="")

    def test_integrity_optional(self):
        assert _run_envelope().integrity is None

    def test_integrity_when_present_validated(self):
        env = _run_envelope(integrity=EnvelopeIntegrity(algorithm="hmac-sha256", key_id="k1", signature="s1"))
        assert env.integrity is not None
        assert env.integrity.algorithm == "hmac-sha256"

    def test_integrity_unknown_algorithm_rejected(self):
        with pytest.raises(ValidationError):
            EnvelopeIntegrity(algorithm="rsa-4096", key_id="k1", signature="s1")  # type: ignore[arg-type]

    def test_release_ref_is_pinned(self):
        # The envelope carries the full ReleaseRef; the executor must consume
        # this, never re-read the channel at execution time (§8.1).
        env = _run_envelope()
        assert env.release_ref.digest == "sha256:abcdef"
        assert env.release_ref.channel == "prod"

    def test_tenant_org_and_release_org_can_differ_surface(self):
        # The DTO does not cross-validate tenant.org_id vs release_ref.org_id
        # here (that is an adapter/RunEnvelope contract test, testing-strategy
        # §7.3). But the surface must carry both so the mismatch is detectable.
        env = _run_envelope(
            tenant=_tenant(ORG_A),
            release_ref=_release_ref(ORG_B),
        )
        assert env.tenant.org_id == ORG_A
        assert env.release_ref.org_id == ORG_B


class TestPolicySnapshotRef:
    def test_policy_version_required(self):
        with pytest.raises(ValidationError):
            PolicySnapshotRef(policy_version="", evaluated_at=TS)

    def test_expires_at_optional(self):
        assert _policy_snapshot().expires_at is None


# ===========================================================================
# ApprovalTicket — CONTRACT-011-APPROVAL
# ===========================================================================


class TestApprovalTicket:
    @pytest.mark.parametrize("status", ["pending", "approved", "rejected", "expired"])
    def test_known_statuses_accepted(self, status):
        assert _approval_ticket(status=status).status == status

    def test_unknown_status_rejected(self):
        with pytest.raises(ValidationError):
            _approval_ticket(status="withdrawn")  # type: ignore[arg-type]

    def test_resume_token_ref_required(self):
        with pytest.raises(ValidationError):
            _approval_ticket(resume_token_ref="")

    def test_expires_at_required(self):
        # Unlike PolicyDecision.expires_at (optional), a ticket must always
        # have a concrete expiry so it cannot wait forever (§9).
        with pytest.raises(ValidationError):
            ApprovalTicket(
                ticket_id="t",
                org_id=ORG_A,
                run_id="r",
                action="a",
                risk_class="high",
                status="pending",
                resume_token_ref="ref",
                created_at=TS,
            )  # missing expires_at


# ===========================================================================
# AuditEvent — CONTRACT-011-AUDIT
# ===========================================================================


class TestAuditEvent:
    @pytest.mark.parametrize("outcome", ["success", "denied", "failure"])
    def test_known_outcomes_accepted(self, outcome):
        assert _audit_event(outcome=outcome).outcome == outcome

    def test_unknown_outcome_rejected(self):
        with pytest.raises(ValidationError):
            _audit_event(outcome="error")  # type: ignore[arg-type]

    def test_event_id_required(self):
        with pytest.raises(ValidationError):
            _audit_event(event_id="")

    def test_org_id_optional_only_for_system_events(self):
        # org_id may be None for documented system-global events (ADR-0002 §4.1);
        # tenant events must set it. The DTO permits None; the audit service
        # enforces the tenant-vs-system rule.
        ae = _audit_event(org_id=None, action="system.break_glass.enabled")
        assert ae.org_id is None

    def test_actor_is_principal_ref(self):
        ae = _audit_event()
        assert isinstance(ae.actor, PrincipalRef)
        assert ae.actor.id == "u-1"

    @pytest.mark.parametrize("forbidden_key", sorted(_FORBIDDEN_PAYLOAD_KEYS))
    def test_forbidden_payload_keys_rejected(self, forbidden_key):
        # Defense-in-depth at the DTO boundary: a producer that accidentally
        # places a secret-bearing key in payload is rejected, not silently
        # scrubbed-and-kept (ADR-0005 §6).
        with pytest.raises(ValidationError):
            _audit_event(payload={forbidden_key: "leaked-value"})

    def test_safe_payload_keys_accepted(self):
        ae = _audit_event(payload={"release_to_version": "1.2.0", "role_id": "r-1"})
        assert ae.payload["release_to_version"] == "1.2.0"


# ===========================================================================
# UsageRecord — CONTRACT-011-USAGE
# ===========================================================================


class TestUsageRecord:
    @pytest.mark.parametrize("status", ["success", "failure", "cancelled"])
    def test_known_statuses_accepted(self, status):
        assert _usage_record(status=status).status == status

    def test_unknown_status_rejected(self):
        with pytest.raises(ValidationError):
            _usage_record(status="timeout")  # type: ignore[arg-type]

    @pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "cached_tokens", "attempt"])
    def test_non_negative_integers(self, field):
        with pytest.raises(ValidationError):
            _usage_record(**{field: -1})

    def test_org_id_required(self):
        with pytest.raises(ValidationError):
            _usage_record(org_id="")

    def test_cost_optional_when_no_price_table(self):
        # Missing price table => cost_* are None, but tokens are never lost (§11).
        ur = _usage_record(cost_amount=None, cost_currency=None)
        assert ur.cost_amount is None
        assert ur.input_tokens == 100

    def test_release_digest_required(self):
        with pytest.raises(ValidationError):
            _usage_record(release_digest="")


# ===========================================================================
# Protocols — CONTRACT-011-PROTO
# ===========================================================================


class TestProtocolsAreUsable:
    """The Protocols are structural types; any object with the right method
    signature satisfies them. This confirms the harness can accept an app-layer
    adapter without importing it."""

    def test_policy_evaluator_protocol_satisfied_by_duck_type(self):
        from deerflow.contracts import PolicyEvaluator

        class AlwaysAllow:
            def evaluate(self, request: PolicyRequest) -> PolicyDecision:
                return _policy_decision()

        evaluator: PolicyEvaluator = AlwaysAllow()  # type-checks structurally
        assert evaluator.evaluate(_policy_request()).decision == "allow"

    def test_release_resolver_protocol_satisfied_by_duck_type(self):
        from deerflow.contracts import ReleaseResolver, TenantContext  # noqa: F401

        class FakeResolver:
            def resolve(self, tenant: TenantContext, agent_name: str, channel: str) -> ReleaseRef:
                return _release_ref(tenant.org_id)

        resolver: ReleaseResolver = FakeResolver()
        assert resolver.resolve(_tenant(), "demo", "prod").agent_name == "demo"

    def test_audit_sink_protocol_satisfied_by_duck_type(self):
        from deerflow.contracts import AuditSink

        captured: list[AuditEvent] = []

        class ListSink:
            def emit(self, event: AuditEvent) -> None:
                captured.append(event)

        sink: AuditSink = ListSink()
        sink.emit(_audit_event())
        assert len(captured) == 1

    def test_usage_recorder_protocol_satisfied_by_duck_type(self):
        from deerflow.contracts import UsageRecorder

        captured: list[UsageRecord] = []

        class ListRecorder:
            def record(self, record: UsageRecord) -> None:
                captured.append(record)

        recorder: UsageRecorder = ListRecorder()
        recorder.record(_usage_record())
        assert len(captured) == 1


# ===========================================================================
# Immutability — CONTRACT-011-IMMUTABLE
# ===========================================================================


class TestImmutability:
    @pytest.mark.parametrize(
        "builder",
        [
            _resource,
            _policy_request,
            _policy_decision,
            _release_ref,
            _policy_snapshot,
            _run_envelope,
            _audit_event,
            _usage_record,
            _approval_ticket,
        ],
        ids=[
            "ResourceRef",
            "PolicyRequest",
            "PolicyDecision",
            "ReleaseRef",
            "PolicySnapshotRef",
            "RunEnvelope",
            "AuditEvent",
            "UsageRecord",
            "ApprovalTicket",
        ],
    )
    def test_model_is_frozen(self, builder):
        obj = builder()
        # frozen models raise ValidationError on attribute assignment
        first_field = next(iter(type(obj).model_fields))
        with pytest.raises(ValidationError):
            setattr(obj, first_field, "MUTATED")

    def test_nested_envelope_fields_are_frozen(self):
        env = _run_envelope()
        with pytest.raises(ValidationError):
            env.release_ref.digest = "tampered"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            env.tenant.org_id = "tampered"  # type: ignore[misc]


# ===========================================================================
# Serialization round-trips — CONTRACT-011-FIXTURE
# ===========================================================================


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


FIXTURE_MATRIX = [
    (PolicyRequest, "policy_request.json"),
    (PolicyDecision, "policy_decision.json"),
    (ReleaseRef, "release_ref.json"),
    (RunEnvelope, "run_envelope.json"),
    (AuditEvent, "audit_event.json"),
    (UsageRecord, "usage_record.json"),
    (ApprovalTicket, "approval_ticket.json"),
]


class TestCanonicalFixtures:
    @pytest.mark.parametrize(("model", "fixture_name"), FIXTURE_MATRIX, ids=[f[1] for f in FIXTURE_MATRIX])
    def test_fixture_loads_into_model(self, model, fixture_name):
        obj = model.model_validate(_load_fixture(fixture_name))
        assert obj is not None

    @pytest.mark.parametrize(("model", "fixture_name"), FIXTURE_MATRIX, ids=[f[1] for f in FIXTURE_MATRIX])
    def test_fixture_round_trips_stably(self, model, fixture_name):
        data = _load_fixture(fixture_name)
        obj = model.model_validate(data)
        round_tripped = model.model_validate(obj.model_dump(mode="json"))
        assert round_tripped == obj

    def test_run_envelope_fixture_integrity_is_none(self):
        # The canonical fixture is a same-DB read, so integrity is None (§6).
        env = RunEnvelope.model_validate(_load_fixture("run_envelope.json"))
        assert env.integrity is None

    def test_audit_fixture_payload_has_no_secrets(self):
        ae = AuditEvent.model_validate(_load_fixture("audit_event.json"))
        for key in ae.payload:
            assert key not in _FORBIDDEN_PAYLOAD_KEYS

    def test_usage_fixture_tokens_non_negative(self):
        ur = UsageRecord.model_validate(_load_fixture("usage_record.json"))
        assert ur.input_tokens >= 0
        assert ur.output_tokens >= 0
        assert ur.cached_tokens >= 0


# ===========================================================================
# Forward compatibility — CONTRACT-011-COMPAT (§13.2)
# ===========================================================================


class TestForwardCompatibility:
    def test_release_ref_ignores_unknown_fields(self):
        data = _load_fixture("release_ref.json") | {"future_field": "x"}
        rr = ReleaseRef.model_validate(data)
        assert not hasattr(rr, "future_field")

    def test_audit_event_ignores_unknown_fields(self):
        data = _load_fixture("audit_event.json") | {"future_field": "x"}
        ae = AuditEvent.model_validate(data)
        assert not hasattr(ae, "future_field")

    def test_missing_required_field_fails(self):
        data = _load_fixture("release_ref.json")
        del data["digest"]
        with pytest.raises(ValidationError):
            ReleaseRef.model_validate(data)
