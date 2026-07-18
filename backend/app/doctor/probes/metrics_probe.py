"""Prometheus metrics presence probe for the production doctor (PR-064).

Implements the ``metrics.presence`` check: verifies the prometheus_client
registry is importable AND that every wired §4 metric name (PR-063) appears
in the generated payload. This is a stronger signal than
``deerflow.observability.metrics.registry_health()`` (which only checks
import-ability) — a registry that is technically importable but has no
collectors means the metric-wiring call sites never ran, which would blind
every §6 SLO dashboard.

In-process by design: the probe calls ``generate_metrics_payload()`` directly
rather than scraping the ``/metrics`` HTTP endpoint. This keeps the doctor
independent of whether the gateway is currently serving (the doctor is a
preflight gate, often run before the gateway starts), while still exercising
the exact code path Prometheus would scrape. When run inside a gateway pod
the global REGISTRY is the same one the pod serves; when run from an
operator host the global REGISTRY contains only the Python-default collectors
(``python_*`` / ``process_*``) and the wired metrics will be missing —
which is the correct FAIL outcome ("doctor not running in gateway pod;
re-run from a gateway pod or accept that metrics cannot be verified").

Disabled-metrics case: ``observability.metrics.enabled=false`` produces a
WARN rather than FAIL — SLO dashboards go dark but the gateway still works,
and the operator may have disabled metrics intentionally (e.g. restricted
ingress). The WARN surfaces the operational consequence (SLO blindness).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_CHECK_ID = "metrics.presence"
_COMPONENT = "observability"
_CONFIG_SOURCE = "config.yaml:observability.metrics,deerflow/observability/metrics.py"

# The wired metric names from PR-063 (mirrors the lazy-singleton accessor list
# in deerflow/observability/metrics.py). Kept explicit rather than derived
# from ``__all__`` so the probe fails closed if a metric is renamed — that
# rename would silently break every dashboard PromQL otherwise. A new metric
# added in a future PR must be added here too (the test
# ``test_doctor_probes.py::TestMetricsProbe::test_expected_names_match_registry``
# cross-checks this list against the live payload).
EXPECTED_METRIC_NAMES: tuple[str, ...] = (
    # §4.2 Gateway
    "http_requests_total",
    "http_request_duration_seconds",
    "http_request_size_bytes",
    "http_response_size_bytes",
    "active_sse_connections",
    "sse_first_business_event_seconds",
    "rate_limit_total",
    # §4.3 Run core
    "runs_created_total",
    "runs_status_total",
    "run_duration_seconds",
    "run_admission_duration_seconds",
    "run_cancel_total",
    "run_reconcile_total",
    "run_reconcile_backlog",
    "worker_active",
    # §4.4 Model / Tool / MCP
    "model_calls_total",
    "model_call_duration_seconds",
    "model_tokens_total",
    "tool_calls_total",
    "tool_call_duration_seconds",
    "mcp_calls_total",
    "mcp_call_duration_seconds",
    # §4.5 Sandbox
    "sandbox_acquire_duration_seconds",
    "sandbox_active",
    "sandbox_pending",
    # §4.6 DB pool / query / transaction
    "db_pool_in_use",
    "db_pool_size",
    "db_query_duration_seconds",
    "db_transaction_failure_total",
)


def _result(status: DoctorStatus, message: str, remediation: str | None = None) -> DoctorCheckResult:
    return DoctorCheckResult(
        check_id=_CHECK_ID,
        status=status,
        component=_COMPONENT,
        message=message,
        remediation=remediation,
        config_source=_CONFIG_SOURCE,
    )


async def probe_metrics_presence(config: AppConfig) -> DoctorCheckResult:
    """Verify the prometheus_client registry is wired and all metrics present.

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Never raises.
    """
    metrics_cfg = config.observability.metrics
    if not metrics_cfg.enabled:
        return _result(
            DoctorStatus.WARN,
            "observability.metrics.enabled=false — Prometheus /metrics endpoint is disabled. Every §6 SLO dashboard will be dark.",
            "Set observability.metrics.enabled=true (default) in production, or accept SLO blindness as an intentional trade-off.",
        )

    try:
        from deerflow.observability.metrics import generate_metrics_payload
    except Exception:  # noqa: BLE001 — observability layer broken is a FAIL
        logger.warning("prometheus_client / observability.metrics not importable", exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            "Could not import deerflow.observability.metrics — the observability layer is broken or prometheus_client is missing.",
            "Reinstall deps (uv sync) and verify the gateway pod imports cleanly; metrics are a §6 SLO prerequisite.",
        )

    try:
        body, _content_type = generate_metrics_payload()
        payload_text = body.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — registry collect failure
        logger.warning("generate_metrics_payload failed", exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            "Prometheus registry failed to generate a payload — a collector raised during collection.",
            "Inspect the gateway logs for the collector that raised; re-run doctor after fixing.",
        )

    missing = [name for name in EXPECTED_METRIC_NAMES if name not in payload_text]
    if missing:
        # If ONLY python_*/process_* are present the probe is running outside a
        # gateway pod (the wired metrics register when the gateway lifespan
        # seeds constant labels + the request path first runs). Surface this
        # distinctly so the operator knows it's an environment issue, not a
        # wiring regression.
        if not any(name in payload_text for name in ("http_requests_total", "runs_created_total")):
            return _result(
                DoctorStatus.WARN,
                (
                    "Only prometheus_client default collectors are registered (no wired metrics "
                    "present). The doctor is likely running outside a gateway pod; re-run from a "
                    f"gateway pod so the request-path metric call sites have executed. ({len(missing)} "
                    "expected metrics missing.)"
                ),
                "Run `make doctor-production` from a gateway pod, or accept that metrics wiring cannot be verified from this host.",
            )
        return _result(
            DoctorStatus.FAIL,
            f"{len(missing)} expected metric(s) missing from the registry payload: {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}.",
            "A metric was renamed or its call site regressed; check deerflow/observability/metrics.py and the EXPECTED_METRIC_NAMES list in this probe.",
        )

    return _result(
        DoctorStatus.PASS,
        f"prometheus_client registry importable and all {len(EXPECTED_METRIC_NAMES)} wired §4 metric names present in the payload.",
    )


__all__ = ["EXPECTED_METRIC_NAMES", "probe_metrics_presence"]
