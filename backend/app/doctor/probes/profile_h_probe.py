"""Profile H HA-readiness probe for the production doctor (PR-074, Track G).

Implements the ``profile_h.ha_readiness`` check: when the deployment declares
``profile: H`` (HA gateway, ``replicas>=2``), verify the *runtime* HA
preconditions are met — not just the static declarations (those are covered by
``check_deployment_profile`` and ``deployment.evidence_validation``). The
load-bearing runtime precondition is **Redis connectivity**: the
ownership/lease layer (PR-071), the SSE cross-replica recovery StreamBridge
(PR-073), and the reconciler (PR-072) all depend on Redis. A Profile H
declaration without Redis configured is a misleading HA claim.

When ``profile != "H"`` the probe **WARN-skips** — dev / single-replica
(Profile S) and physical-split (Profile W) topologies do not require the
in-process HA readiness check (Profile W has its own evidence requirements).
This mirrors the ``redis.connectivity`` probe's WARN-skip convention.

The probe is pure config validation — no DB, no live Redis PING (that is
``redis.connectivity``'s job). It runs unconditionally (regardless of gateway
URL) but short-circuits to WARN for non-H profiles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

_CHECK_ID = "profile_h.ha_readiness"
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


async def probe_profile_h_readiness(config: AppConfig) -> DoctorCheckResult:
    """Verify Profile H runtime HA readiness (ADR-0006 §2.1/§3.5/§11).

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Never raises.
    """
    deployment = config.production.deployment
    profile = deployment.profile

    if profile != "H":
        return _result(
            DoctorStatus.WARN,
            f"deployment.profile={profile!r} — Profile H HA-readiness check skipped (only meaningful for profile=H).",
            None,
        )

    # Profile H requires Redis for ownership/lease (PR-071) + SSE recovery
    # (PR-073) + reconciler coordination (PR-072). Without it the HA claim is
    # misleading — the static check may PASS on declarations alone, but the
    # runtime cannot honour single-owner semantics across replicas.
    redis_cfg = getattr(config.production, "redis", None)
    redis_url = getattr(redis_cfg, "url", None) if redis_cfg is not None else None
    if not redis_url:
        return _result(
            DoctorStatus.FAIL,
            "deployment.profile=H requires production.redis.url — the ownership/lease + SSE-recovery + reconciler stack (PR-071/072/073) all depend on Redis for cross-replica coordination.",
            "Set production.redis.url (redis:// or rediss://) in config.yaml. Without Redis, Profile H cannot enforce single-owner semantics across replicas (ADR-0006 §4).",
        )

    # Declaration completeness (redundant with the static check, but the LIVE
    # report is more actionable — it names the exact missing field and runs
    # alongside the other live probes an operator reviews together).
    missing: list[str] = []
    if deployment.profile_h_soak_hours < 24:
        missing.append("profile_h_soak_hours (>=24)")
    if not deployment.profile_h_fault_injection_evidence:
        missing.append("profile_h_fault_injection_evidence")
    if missing:
        return _result(
            DoctorStatus.FAIL,
            f"deployment.profile=H with Redis configured, but missing HA admission evidence: {', '.join(missing)}.",
            "Complete the 24h HA soak + production-equivalent fault-injection drill, then set the missing fields under production.deployment in config.yaml (ADR-0006 §3.5/§11).",
        )

    return _result(
        DoctorStatus.PASS,
        "deployment.profile=H: Redis configured and HA admission evidence (soak hours + fault-injection record) declared. Real 24h soak runs in the release pipeline; this probe verifies the operator declaration is complete.",
    )


__all__ = ["probe_profile_h_readiness"]
