"""Production declaration schema and fail-closed doctor tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import doctor
import pytest
from pydantic import ValidationError

from app.doctor.models import DoctorCheckResult, DoctorReport, DoctorStatus
from app.doctor.production import DEFERRED_LIVE_CHECKS, STATIC_CHECKS, run_production_checks
from deerflow.config.app_config import AppConfig
from deerflow.config.production_config import ProductionConfig


def _production_data() -> dict:
    return {
        "log_level": "info",
        "sandbox": {
            "use": "deerflow.community.aio_sandbox:AioSandboxProvider",
            "allow_host_bash": False,
            "provisioner_url": "http://sandbox-provisioner:8002",
            "replicas": 3,
        },
        "database": {
            "backend": "postgres",
            "postgres_url": "postgresql://example.invalid/deernexus",
            "pool_size": 5,
        },
        "run_events": {"backend": "db"},
        "production": {
            "enabled": True,
            "environment": "production",
            "deployment": {
                "profile": "H",
                "gateway_replicas": 2,
                "profile_h_evidence": "evidence://profile-h-validation",
            },
            "oidc": {
                "issuer": "https://identity.example.com",
                "audience": "deernexus",
            },
            "redis": {"url": "rediss://redis.example.invalid:6379/0"},
            "backup": {
                "enabled": True,
                "declared_rpo_hours": 24,
            },
            "secret_store": {
                "provider": "vault",
                "references_only": True,
            },
            "limits": {
                "max_concurrent_runs": 10,
                "max_sandbox_replicas": 3,
            },
            "gateway_security": {
                "tls_enabled": True,
                "cors_origins": ["https://deernexus.example.com"],
                "csrf_enabled": True,
                "rate_limit_enabled": True,
            },
            "log_redaction_enabled": True,
        },
    }


def _config(mutator=None) -> AppConfig:
    data = copy.deepcopy(_production_data())
    if mutator:
        mutator(data)
    return AppConfig.model_validate(data)


def _raw_config_data() -> dict:
    data = copy.deepcopy(_production_data())
    data["database"]["postgres_url"] = "$DATABASE_URL"
    data["production"]["redis"]["url"] = "$REDIS_URL"
    return data


def _run_checks(config: AppConfig, raw_config: dict | None = None) -> DoctorReport:
    return run_production_checks(config, Path("config.yaml"), raw_config or _raw_config_data())


def _by_id(report: DoctorReport, check_id: str) -> DoctorCheckResult:
    return next(check for check in report.checks if check.check_id == check_id)


def test_production_schema_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ProductionConfig.model_validate({"unknown": True})


def test_app_config_defaults_to_disabled_production_declarations():
    config = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}})

    assert config.production.enabled is False
    assert config.production.environment == "development"


def test_static_declarations_pass_but_deferred_live_probes_block(monkeypatch):
    monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)
    report = _run_checks(_config())

    static_count = len(STATIC_CHECKS) + 1
    assert all(check.status is DoctorStatus.PASS for check in report.checks[:static_count])
    assert all(check.status is DoctorStatus.FAIL for check in report.checks[static_count:])
    assert report.fail_count == len(DEFERRED_LIVE_CHECKS)
    assert report.ready is False
    assert report.exit_code == 1


def test_deferred_live_checks_have_track_specific_remediation():
    """PR-064: every deferred check must carry a Track-specific remediation,
    not the pre-PR-064 generic 'Implement and verify this live probe in
    PR-064' placeholder. An operator reading the report should know exactly
    which Track / PR is blocking each check."""
    report = _run_checks(_config())
    deferred = {c.check_id: c for c in report.checks if c.check_id in {row[0] for row in DEFERRED_LIVE_CHECKS}}
    assert len(deferred) == len(DEFERRED_LIVE_CHECKS)
    for check_id, check in deferred.items():
        # The generic placeholder is gone — every remediation must name a
        # concrete Track / PR / code-path-missing reason.
        assert check.remediation is not None
        assert "Implement and verify this live probe in PR-064" not in check.remediation, f"{check_id} still has the generic PR-064 placeholder remediation"
        # Each remediation must reference either a Track, a PR number, or
        # "Blocked on" — the marker that distinguishes PR-064's specific
        # blocker from a generic stub.
        assert "Blocked on" in check.remediation, f"{check_id} remediation must explain the specific blocker (expected 'Blocked on …')"


def test_host_bash_blocks_production():
    report = _run_checks(_config(lambda data: data["sandbox"].update({"allow_host_bash": True})))

    assert _by_id(report, "sandbox.isolated").status is DoctorStatus.FAIL


def test_auth_disabled_blocks_production(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")

    report = _run_checks(_config())

    assert _by_id(report, "auth.enabled").status is DoctorStatus.FAIL


def test_literal_database_or_redis_credentials_are_rejected():
    raw_config = _raw_config_data()
    raw_config["database"]["postgres_url"] = "postgresql://example.invalid/deernexus"

    report = _run_checks(_config(), raw_config)

    assert _by_id(report, "secrets.references_only").status is DoctorStatus.FAIL


def test_profile_h_rejects_physical_worker_declaration():
    def profile_h_with_worker(data: dict) -> None:
        data["production"]["deployment"]["worker_replicas"] = 1

    report = _run_checks(_config(profile_h_with_worker))

    assert _by_id(report, "deployment.profile_consistency").status is DoctorStatus.FAIL


def test_profile_s_requires_registered_ha_waiver():
    def profile_s(data: dict) -> None:
        data["production"]["deployment"] = {
            "profile": "S",
            "gateway_replicas": 1,
            "worker_replicas": 0,
        }

    report = _run_checks(_config(profile_s))
    assert _by_id(report, "deployment.profile_consistency").status is DoctorStatus.FAIL

    def profile_s_with_waiver(data: dict) -> None:
        profile_s(data)
        data["production"]["deployment"]["ha_waiver_id"] = "WAIVER-001"

    report = _run_checks(_config(profile_s_with_waiver))
    assert _by_id(report, "deployment.profile_consistency").status is DoctorStatus.WARN


def test_profile_w_requires_gateway_profile_soak_and_rollback_evidence():
    def profile_w(data: dict) -> None:
        data["production"]["deployment"] = {
            "profile": "W",
            "gateway_profile": "S",
            "gateway_replicas": 1,
            "worker_replicas": 2,
            "profile_w_evidence": "evidence://profile-w-validation",
            "profile_w_rollback_evidence": "evidence://profile-w-rollback",
            "profile_w_soak_hours": 23,
        }

    report = _run_checks(_config(profile_w))

    assert _by_id(report, "deployment.profile_consistency").status is DoctorStatus.FAIL


def test_profile_w_with_profile_s_gateway_requires_ha_waiver():
    def profile_w(data: dict) -> None:
        data["production"]["deployment"] = {
            "profile": "W",
            "gateway_profile": "S",
            "gateway_replicas": 1,
            "worker_replicas": 2,
            "profile_w_evidence": "evidence://profile-w-validation",
            "profile_w_rollback_evidence": "evidence://profile-w-rollback",
            "profile_w_soak_hours": 24,
        }

    report = _run_checks(_config(profile_w))
    assert _by_id(report, "deployment.profile_consistency").status is DoctorStatus.FAIL

    def profile_w_with_waiver(data: dict) -> None:
        profile_w(data)
        data["production"]["deployment"]["ha_waiver_id"] = "WAIVER-002"

    report = _run_checks(_config(profile_w_with_waiver))
    assert _by_id(report, "deployment.profile_consistency").status is DoctorStatus.WARN


@pytest.mark.parametrize(
    ("mutator", "check_id"),
    [
        (lambda data: data["production"]["redis"].update({"url": "redis://plaintext.invalid"}), "redis.declared"),
        (lambda data: data["production"]["oidc"].update({"issuer": "http://identity.invalid"}), "oidc.declared"),
        (
            lambda data: data["production"]["gateway_security"].update({"cors_origins": ["http://deernexus.invalid"]}),
            "security.production_baseline",
        ),
    ],
)
def test_plaintext_production_endpoints_are_rejected(mutator, check_id: str):
    report = _run_checks(_config(mutator))

    assert _by_id(report, check_id).status is DoctorStatus.FAIL


def test_all_runbook_placeholders_remain_fail_closed():
    # PR-064 converted 5 former placeholders into real live probes
    # (postgres.connectivity, metrics.presence, deployment.evidence_validation,
    # gateway.security_validation, gateway.rate_limit_retry_after) — those are
    # no longer in DEFERRED_LIVE_CHECKS and are tested in test_doctor_probes.py.
    # PR-042 promoted audit.outbox to a live probe (probe_audit_outbox); it is
    # tested in test_doctor_probes.py and no longer in DEFERRED_LIVE_CHECKS.
    # What remains here are the checks whose code paths do not exist yet;
    # they MUST stay FAIL until their owning Track lands, so an operator
    # never sees a misleading green on an unverified invariant.
    expected = {
        "redis.connectivity",
        "oidc.jwks_validation",
        "sandbox.provisioner_create",
        "backup.freshness",
        "secret_store.access",
        "object_storage.security",
        "agent.release_ref_enforcement",
    }

    report = _run_checks(_config())
    checks = {check.check_id: check for check in report.checks}

    assert expected <= checks.keys()
    assert all(checks[check_id].status is DoctorStatus.FAIL for check_id in expected)


def test_report_json_never_contains_configured_secret_values():
    secret_value = "postgresql://example.invalid/deernexus?marker=sensitive-value"
    report = _run_checks(_config(lambda data: data["database"].update({"postgres_url": secret_value})))

    payload = json.dumps(report.to_dict())

    assert secret_value not in payload
    assert {"check_id", "status", "component", "message", "remediation", "config_source", "timestamp"} <= set(report.to_dict()["checks"][0])


def test_production_cli_json_uses_report_exit_code(monkeypatch, capsys):
    report = DoctorReport(
        profile="production",
        config_path="config.yaml",
        checks=(
            DoctorCheckResult(
                check_id="example.blocker",
                status=DoctorStatus.FAIL,
                component="example",
                message="blocked",
                remediation="fix it",
                config_source="config.yaml:example",
            ),
        ),
    )

    async def _fake_run(_path):
        return report

    monkeypatch.setattr(doctor, "_run_production_doctor", _fake_run)

    exit_code = doctor.main(["--profile", "production", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["ready"] is False
    assert output["checks"][0]["status"] == "FAIL"
