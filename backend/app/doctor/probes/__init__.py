"""Live probes for the production doctor (PR-064).

Each probe is an ``async def probe_xxx(config) -> DoctorCheckResult`` that
mirrors the ``tenant_probe`` pattern (throwaway / in-process resources, all
external failures contained into a FAIL result, never raises, never leaks
secrets). The CLI awaits them and passes their results via
``run_production_checks(..., extra_checks=...)`` — ``run_production_checks``
itself stays synchronous.

Probes delivered in PR-064 (those whose code paths exist today):

* :mod:`postgres_probe` — live DB connectivity, version ≥15, pool stats.
* :mod:`metrics_probe` — Prometheus registry health + expected metric names.
* :mod:`deployment_evidence_probe` — Profile H/W evidence-link presence.
* :mod:`gateway_security_probe` — live TLS/CORS/CSRF HTTP probe.
* :mod:`rate_limit_probe` — live 429 + Retry-After probe.

Deferred probes (no code path today) remain as ``DEFERRED_LIVE_CHECKS`` FAIL
stubs in ``app/doctor/production.py`` with Track-specific remediation — see
``runtime-contracts.md §16.28`` for the per-Track blocker list.
"""

from app.doctor.probes.audit_probe import probe_audit_outbox
from app.doctor.probes.deployment_evidence_probe import probe_deployment_evidence
from app.doctor.probes.gateway_security_probe import probe_gateway_security
from app.doctor.probes.metrics_probe import probe_metrics_presence
from app.doctor.probes.postgres_probe import probe_postgres_connectivity
from app.doctor.probes.rate_limit_probe import probe_rate_limit_retry_after

__all__ = [
    "probe_audit_outbox",
    "probe_deployment_evidence",
    "probe_gateway_security",
    "probe_metrics_presence",
    "probe_postgres_connectivity",
    "probe_rate_limit_retry_after",
]
