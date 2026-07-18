"""Deployment Profile evidence probe for the production doctor (PR-064).

Implements ``deployment.evidence_validation``: validates that the configured
``production.deployment`` profile carries the evidence links / soak hours
each profile level requires per runbook §5.1.

Profile S (single replica): no extra evidence required → PASS by default.

Profile H (HA gateway): requires ``profile_h_evidence`` — a non-empty
documentation/decision link justifying the HA waiver or topology. The
field is operator-supplied (a runbook URL, a design doc, or an HA-test
report); the probe only verifies presence and basic string shape, not
HTTP reachability (that is the release pipeline's job, not the doctor's).

Profile W (worker split): requires three pieces of evidence —
``profile_w_evidence`` (worker dispatch decision), ``profile_w_rollback_evidence``
(rollback procedure), and ``profile_w_soak_hours > 0`` (the documented soak
duration). Missing any one is a FAIL per runbook §5.1 because Profile W is
the most operationally complex topology and undocumented dispatch/rollback
is a known incident cause.

This probe is pure config validation — no DB, no HTTP. It runs unconditionally
(regardless of gateway URL).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

_CHECK_ID = "deployment.evidence_validation"
_COMPONENT = "deployment"
_CONFIG_SOURCE = "config.yaml:production.deployment"


def _result(status: DoctorStatus, message: str, remediation: str | None = None) -> DoctorCheckResult:
    return DoctorCheckResult(
        check_id=_CHECK_ID,
        status=status,
        component=_COMPONENT,
        message=message,
        remediation=remediation,
        config_source=_CONFIG_SOURCE,
    )


def _is_non_empty_evidence(value: object) -> bool:
    """Return True if *value* looks like a real evidence link / doc reference.

    Accepts any non-empty string; the doctor does not validate URL format
    (an internal doc path or ticket id is as valid as an https URL).
    """
    return isinstance(value, str) and value.strip() != ""


async def probe_deployment_evidence(config: AppConfig) -> DoctorCheckResult:
    """Validate deployment-profile evidence fields per runbook §5.1.

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Pure config check —
    never raises.
    """
    deployment = config.production.deployment
    profile = deployment.profile

    if profile == "S":
        return _result(
            DoctorStatus.PASS,
            "deployment.profile=S (single replica) — no additional HA/worker evidence required by runbook §5.1.",
        )

    if profile == "H":
        if not _is_non_empty_evidence(deployment.profile_h_evidence):
            return _result(
                DoctorStatus.FAIL,
                "deployment.profile=H (HA gateway) requires production.deployment.profile_h_evidence to be set (a runbook URL, design doc, or HA-test report justifying HA topology).",
                "Set profile_h_evidence under production.deployment in config.yaml; Profile H cannot enter production admission without documented HA evidence.",
            )
        return _result(
            DoctorStatus.PASS,
            "deployment.profile=H and profile_h_evidence is set.",
        )

    if profile == "W":
        missing: list[str] = []
        if not _is_non_empty_evidence(deployment.profile_w_evidence):
            missing.append("profile_w_evidence")
        if not _is_non_empty_evidence(deployment.profile_w_rollback_evidence):
            missing.append("profile_w_rollback_evidence")
        if deployment.profile_w_soak_hours <= 0:
            missing.append("profile_w_soak_hours (>0)")
        if missing:
            return _result(
                DoctorStatus.FAIL,
                f"deployment.profile=W (worker split) requires all of profile_w_evidence / profile_w_rollback_evidence / profile_w_soak_hours(>0); missing: {', '.join(missing)}.",
                "Set the missing Profile W evidence fields under production.deployment in config.yaml; Profile W is the most operationally complex topology and undocumented dispatch/rollback is a known incident cause.",
            )
        return _result(
            DoctorStatus.PASS,
            f"deployment.profile=W with evidence + rollback evidence + soak_hours={deployment.profile_w_soak_hours}h all set.",
        )

    # Defensive — schema validates profile ∈ {S,H,W}, but fail-closed anyway.
    return _result(
        DoctorStatus.FAIL,
        f"deployment.profile={profile!r} is not a recognised value (expected S/H/W).",
        "Set production.deployment.profile to one of S/H/W in config.yaml.",
    )


__all__ = ["probe_deployment_evidence"]
