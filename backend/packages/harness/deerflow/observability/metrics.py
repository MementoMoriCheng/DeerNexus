"""Prometheus metrics registry + ``/metrics`` scrape helper (PR-063).

Implements ``docs/ops/observability-and-slo.md`` §4 (metrics) + §4.1 (label
cardinality rules) on top of PR-062's ``deerflow/observability`` package.
The registry is the single source of truth for which metrics exist and which
labels they accept; call sites go through the typed accessors below rather
than reaching into ``prometheus_client`` directly so the §4.1 allow-list gate
stays in one place.

Why a registry layer and not bare ``prometheus_client`` symbols:

* **§4.1 cardinality guard**: every metric's labelnames is pinned here and a
  unit test (``test_observability_metrics.py``) asserts no high-cardinality id
  (``org_id`` / ``user_id`` / ``run_id`` / ``request_id`` / ``trace_id`` /
  ``thread_id`` / ``raw_url`` / ``artifact_name``) leaks in. Bare symbols
  scattered across 15+ files would make the audit impossible.
* **Lazy singletons**: ``prometheus_client`` registers each metric name once
  per process on the global ``REGISTRY``; a duplicate ``Counter(...)`` call
  with the same name raises at import time. The ``@lru_cache`` accessors
  below make every call site idempotent without each one re-implementing the
  singleton dance.
* **Fail-open**: a label-value type error or a registry conflict must never
  break the request. Every public accessor wraps the prometheus call in
  ``try/except`` (observability is never a correctness gate — same contract as
  ``emit_event`` in PR-062).
* **Test isolation**: the accessors accept an optional ``registry`` parameter
  so unit tests can pass a fresh ``CollectorRegistry`` per test instead of
  polluting the process-global one.

Deferred metrics (no code path today, NOT registered to avoid empty forever-0
counters per §7.1 "不在缺少真实基线时伪设精确告警"):

* §4.3 Profile-W HA: ``run_terminal_convergence_seconds``,
  ``run_ownership_acquire_total``, ``run_ownership_conflict_total``,
  ``run_lease_expired_total``, ``run_heartbeat_failure_total``,
  ``worker_claim_total``, ``worker_dead_letter_total``,
  ``run_dispatch_backlog``, ``run_dispatch_oldest_age_seconds`` — need
  ownership / lease / heartbeat / message-queue code paths (future HA PR).
* §4.5 Sandbox hardening: ``sandbox_oom_total``, ``sandbox_quarantine_total``,
  ``sandbox_timeout_total``, ``sandbox_cleanup_failure_total`` — need OOM
  detection / quarantine / timeout (Track E).
* §4.6 Data & Audit: ``redis_*`` (no Redis client),
  ``audit_outbox_*`` / ``audit_dead_letter_*`` (PR-041 — now registered),
  ``audit_archive_lag_seconds`` (no archive job — PR-045),
  ``usage_ingest_lag_seconds``, ``object_digest_mismatch_total``,
  ``backup_last_success_timestamp`` (PR-065 — now registered).
* §4.4 ``model_cost_amount`` (no price table), ``policy_decisions_total``
  (no Policy engine — Track C).
* §4.2 ``oidc_login_total`` (no OIDC code path — only local login exists).

See ``runtime-contracts.md §16.26`` for the full deferred list.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache
from typing import Any, Final

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# §4.1 label allow-list (cardinality guard)
# ---------------------------------------------------------------------------
#
# observability-and-slo §4.1 enumerates the only label names a public metric
# may carry. Every metric registered below declares a subset of these — the
# ``test_labelnames_are_all_allow_listed`` test pins that no metric sneaks in
# a high-cardinality id. Adding a label requires (1) adding it here and (2)
# updating the test; the friction is the audit.

ALLOWED_LABELS: Final[frozenset[str]] = frozenset(
    {
        "service",
        "environment",
        "deployment_version",
        "route_template",
        "method",
        "status_class",
        "error_code",
        "run_status",
        "tool_name",
        "model",
        "provider",
        "channel",
        "outcome",
        # Internal-use only (not in §4.1's public list but required for the
        # counter to be useful). Each is documented at its metric's definition
        # and is bounded (low-cardinality, controlled vocabulary).
        "direction",  # model_tokens_total: "in" / "out" (closed 2-value set)
        "terminal_status",  # run_duration_seconds: final RunStatus value (closed enum)
        "reason",  # rate_limit_total: controlled vocab (e.g. "auth_login_lockout", "api")
        "error_class",  # db_transaction_failure_total: exception class name (bounded by codebase)
    }
)

# Histogram bucket defaults. Latency histograms share these buckets (seconds)
# so dashboards can overlay §6 SLO thresholds (admission P95<3s, SSE P95<5s,
# console P95<0.5s, redis-stream P95<2s) on the same axes.
_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

# Byte-size histogram buckets (request / response size).
_SIZE_BUCKETS: Final[tuple[float, ...]] = (
    100.0,
    1_000.0,
    10_000.0,
    100_000.0,
    1_000_000.0,
    10_000_000.0,
    100_000_000.0,
)

# Default constant labels stamped on every metric so a scrape from any pod is
# self-identifying. Mirrors the OTel Resource attributes from PR-062's
# ``tracing.init_tracing`` (service.name / deployment.environment /
# service.version). Resolved lazily so a config reload is picked up.
_CONSTANT_LABELS_CACHE: dict[str, str] = {}


def _set_constant_labels(service: str, environment: str, deployment_version: str) -> None:
    """Seed the constant labels stamped on every metric.

    Called once from the gateway lifespan (after config load) so the registry
    reflects the operator's ``observability.service_name`` / ``environment`` /
    ``deployment_version``. Idempotent. Empty ``deployment_version`` suppresses
    the label (matches the JsonFormatter behaviour from PR-062).
    """
    new = {"service": service, "environment": environment}
    if deployment_version:
        new["deployment_version"] = deployment_version
    _CONSTANT_LABELS_CACHE.clear()
    _CONSTANT_LABELS_CACHE.update(new)


def _constant_labels() -> dict[str, str]:
    """Return the current constant labels (snapshot)."""
    return dict(_CONSTANT_LABELS_CACHE)


def _registry_or_default(registry: Any) -> Any:
    """Return *registry* or the prometheus_client process-global REGISTRY."""
    if registry is not None:
        return registry
    from prometheus_client import REGISTRY

    return REGISTRY


def _validate_labelnames(name: str, labelnames: tuple[str, ...]) -> None:
    """Assert every label is on the §4.1 allow-list (programming-error guard).

    Raises ``ValueError`` rather than silently dropping — a new label that
    bypasses the audit is a bug, not a runtime condition. The
    ``test_labelnames_are_all_allow_listed`` test pins this at import time so
    the failure surfaces in CI rather than at first scrape.
    """
    for label in labelnames:
        if label not in ALLOWED_LABELS:
            raise ValueError(f"metric {name!r} declares label {label!r} which is not on the §4.1 allow-list (high-cardinality id labels are forbidden on public metrics). Add it to ALLOWED_LABELS only if it is low-cardinality.")


# ---------------------------------------------------------------------------
# Registry accessors — lazy singletons per metric name
# ---------------------------------------------------------------------------
#
# Each accessor uses ``@lru_cache`` keyed on the optional ``registry`` argument
# so the first call constructs the metric on the registry and every subsequent
# call returns the same instance. Passing ``registry=None`` (the common case)
# uses the process-global REGISTRY; tests pass a fresh ``CollectorRegistry``.


def _make_counter(
    name: str,
    description: str,
    labelnames: tuple[str, ...],
    registry: Any,
) -> Any:
    from prometheus_client import Counter

    _validate_labelnames(name, labelnames)
    return Counter(name, description, labelnames + _constant_label_keys(), registry=registry)


def _make_histogram(
    name: str,
    description: str,
    labelnames: tuple[str, ...],
    registry: Any,
    buckets: tuple[float, ...],
) -> Any:
    from prometheus_client import Histogram

    _validate_labelnames(name, labelnames)
    return Histogram(
        name,
        description,
        labelnames + _constant_label_keys(),
        registry=registry,
        buckets=buckets,
    )


def _make_gauge(
    name: str,
    description: str,
    labelnames: tuple[str, ...],
    registry: Any,
) -> Any:
    from prometheus_client import Gauge

    _validate_labelnames(name, labelnames)
    return Gauge(name, description, labelnames + _constant_label_keys(), registry=registry)


def _constant_label_keys() -> tuple[str, ...]:
    """Tuple form of the constant-label keys (sorted for deterministic order)."""
    return tuple(sorted(_CONSTANT_LABELS_CACHE.keys()))


def _constant_label_values() -> dict[str, str]:
    return dict(_CONSTANT_LABELS_CACHE)


def _with_constants(labels: dict[str, str] | None) -> dict[str, str]:
    """Merge *labels* with the constant labels (constant wins on conflict)."""
    merged = _constant_label_values()
    if labels:
        merged.update(labels)
    return merged


# ===========================================================================
# §4.2 Gateway HTTP metrics
# ===========================================================================


@lru_cache(maxsize=8)
def _http_requests_total(registry: Any) -> Any:
    return _make_counter(
        "http_requests_total",
        "Total HTTP requests by method/route/status_class (observability-and-slo §4.2).",
        ("method", "route_template", "status_class", "error_code"),
        registry,
    )


@lru_cache(maxsize=8)
def _http_request_duration_seconds(registry: Any) -> Any:
    return _make_histogram(
        "http_request_duration_seconds",
        "HTTP request latency in seconds (observability-and-slo §4.2).",
        ("method", "route_template", "status_class"),
        registry,
        _LATENCY_BUCKETS,
    )


@lru_cache(maxsize=8)
def _http_request_size_bytes(registry: Any) -> Any:
    return _make_histogram(
        "http_request_size_bytes",
        "HTTP request body size in bytes from Content-Length (observability-and-slo §4.2).",
        ("method", "route_template"),
        registry,
        _SIZE_BUCKETS,
    )


@lru_cache(maxsize=8)
def _http_response_size_bytes(registry: Any) -> Any:
    return _make_histogram(
        "http_response_size_bytes",
        "HTTP response body size in bytes from Content-Length (observability-and-slo §4.2).",
        ("method", "route_template", "status_class"),
        registry,
        _SIZE_BUCKETS,
    )


@lru_cache(maxsize=8)
def _active_sse_connections(registry: Any) -> Any:
    return _make_gauge(
        "active_sse_connections",
        "Currently open SSE consumer streams (observability-and-slo §4.2).",
        (),
        registry,
    )


@lru_cache(maxsize=8)
def _sse_first_business_event_seconds(registry: Any) -> Any:
    return _make_histogram(
        "sse_first_business_event_seconds",
        "Seconds from run creation to first non-heartbeat business event (observability-and-slo §4.2 / §6.4).",
        (),
        registry,
        _LATENCY_BUCKETS,
    )


@lru_cache(maxsize=8)
def _rate_limit_total(registry: Any) -> Any:
    return _make_counter(
        "rate_limit_total",
        "Total rate-limit rejections by reason (observability-and-slo §4.2).",
        ("reason",),
        registry,
    )


def record_http_request(
    *,
    method: str,
    route_template: str,
    status_class: str,
    error_code: str | None,
    duration_seconds: float,
    request_size_bytes: int | None = None,
    response_size_bytes: int | None = None,
    registry: Any = None,
) -> None:
    """Bump the §4.2 HTTP request counters/histograms for one completed request.

    Fail-open: any prometheus error is contained (observability never breaks
    the request — same contract as ``emit_event``).
    """
    try:
        labels = {
            "method": method,
            "route_template": route_template,
            "status_class": status_class,
            "error_code": error_code or "",
        }
        reg = _registry_or_default(registry)
        _http_requests_total(reg).labels(**_with_constants(labels)).inc()
        duration_labels = {k: labels[k] for k in ("method", "route_template", "status_class")}
        _http_request_duration_seconds(reg).labels(**_with_constants(duration_labels)).observe(duration_seconds)
        if request_size_bytes is not None:
            _http_request_size_bytes(reg).labels(**_with_constants({"method": method, "route_template": route_template})).observe(float(request_size_bytes))
        if response_size_bytes is not None:
            _http_response_size_bytes(reg).labels(**_with_constants(duration_labels)).observe(float(response_size_bytes))
    except Exception:  # noqa: BLE001 — fail-open; never break the request
        logger.debug("record_http_request failed", exc_info=True)


def inc_active_sse_connections(registry: Any = None) -> None:
    try:
        _active_sse_connections(_registry_or_default(registry)).labels(**_with_constants({})).inc()
    except Exception:  # noqa: BLE001
        logger.debug("inc_active_sse_connections failed", exc_info=True)


def dec_active_sse_connections(registry: Any = None) -> None:
    try:
        _active_sse_connections(_registry_or_default(registry)).labels(**_with_constants({})).dec()
    except Exception:  # noqa: BLE001
        logger.debug("dec_active_sse_connections failed", exc_info=True)


def observe_sse_first_business_event(seconds: float, registry: Any = None) -> None:
    try:
        _sse_first_business_event_seconds(_registry_or_default(registry)).labels(**_with_constants({})).observe(seconds)
    except Exception:  # noqa: BLE001
        logger.debug("observe_sse_first_business_event failed", exc_info=True)


def inc_rate_limit(*, reason: str, registry: Any = None) -> None:
    try:
        _rate_limit_total(_registry_or_default(registry)).labels(**_with_constants({"reason": reason})).inc()
    except Exception:  # noqa: BLE001
        logger.debug("inc_rate_limit failed", exc_info=True)


# ===========================================================================
# §4.3 Run metrics
# ===========================================================================


@lru_cache(maxsize=8)
def _runs_created_total(registry: Any) -> Any:
    return _make_counter(
        "runs_created_total",
        "Total runs created (observability-and-slo §4.3). Denominator of §6.2 run-create SLO.",
        (),
        registry,
    )


@lru_cache(maxsize=8)
def _runs_status_total(registry: Any) -> Any:
    return _make_counter(
        "runs_status_total",
        "Total run status transitions by terminal status (observability-and-slo §4.3).",
        ("run_status",),
        registry,
    )


@lru_cache(maxsize=8)
def _run_duration_seconds(registry: Any) -> Any:
    return _make_histogram(
        "run_duration_seconds",
        "Wall-clock run duration in seconds by terminal status (observability-and-slo §4.3).",
        ("terminal_status",),
        registry,
        _LATENCY_BUCKETS,
    )


@lru_cache(maxsize=8)
def _run_admission_duration_seconds(registry: Any) -> Any:
    return _make_histogram(
        "run_admission_duration_seconds",
        "Seconds from run-create acceptance to running state (observability-and-slo §4.3 / §6.3).",
        (),
        registry,
        _LATENCY_BUCKETS,
    )


@lru_cache(maxsize=8)
def _run_cancel_total(registry: Any) -> Any:
    return _make_counter(
        "run_cancel_total",
        "Total runs cancelled by the user (observability-and-slo §4.3).",
        (),
        registry,
    )


@lru_cache(maxsize=8)
def _run_reconcile_total(registry: Any) -> Any:
    return _make_counter(
        "run_reconcile_total",
        "Total runs the reconciler acted on by outcome (observability-and-slo §4.3).",
        ("outcome",),
        registry,
    )


@lru_cache(maxsize=8)
def _run_reconcile_backlog(registry: Any) -> Any:
    return _make_gauge(
        "run_reconcile_backlog",
        "Count of inflight runs the reconciler is iterating (observability-and-slo §4.3).",
        (),
        registry,
    )


@lru_cache(maxsize=8)
def _worker_active(registry: Any) -> Any:
    return _make_gauge(
        "worker_active",
        "Count of pending+running runs with a live asyncio task (observability-and-slo §4.3).",
        (),
        registry,
    )


def inc_runs_created(registry: Any = None) -> None:
    try:
        _runs_created_total(_registry_or_default(registry)).labels(**_with_constants({})).inc()
    except Exception:  # noqa: BLE001
        logger.debug("inc_runs_created failed", exc_info=True)


def inc_runs_status(*, run_status: str, registry: Any = None) -> None:
    try:
        _runs_status_total(_registry_or_default(registry)).labels(**_with_constants({"run_status": run_status})).inc()
    except Exception:  # noqa: BLE001
        logger.debug("inc_runs_status failed", exc_info=True)


def observe_run_duration(*, terminal_status: str, seconds: float, registry: Any = None) -> None:
    try:
        _run_duration_seconds(_registry_or_default(registry)).labels(**_with_constants({"terminal_status": terminal_status})).observe(seconds)
    except Exception:  # noqa: BLE001
        logger.debug("observe_run_duration failed", exc_info=True)


def observe_run_admission_duration(seconds: float, registry: Any = None) -> None:
    try:
        _run_admission_duration_seconds(_registry_or_default(registry)).labels(**_with_constants({})).observe(seconds)
    except Exception:  # noqa: BLE001
        logger.debug("observe_run_admission_duration failed", exc_info=True)


def inc_run_cancel(registry: Any = None) -> None:
    try:
        _run_cancel_total(_registry_or_default(registry)).labels(**_with_constants({})).inc()
    except Exception:  # noqa: BLE001
        logger.debug("inc_run_cancel failed", exc_info=True)


def inc_run_reconcile(*, outcome: str, registry: Any = None) -> None:
    try:
        _run_reconcile_total(_registry_or_default(registry)).labels(**_with_constants({"outcome": outcome})).inc()
    except Exception:  # noqa: BLE001
        logger.debug("inc_run_reconcile failed", exc_info=True)


def set_run_reconcile_backlog(count: int, registry: Any = None) -> None:
    try:
        _run_reconcile_backlog(_registry_or_default(registry)).labels(**_with_constants({})).set(count)
    except Exception:  # noqa: BLE001
        logger.debug("set_run_reconcile_backlog failed", exc_info=True)


def set_worker_active(count: int, registry: Any = None) -> None:
    try:
        _worker_active(_registry_or_default(registry)).labels(**_with_constants({})).set(count)
    except Exception:  # noqa: BLE001
        logger.debug("set_worker_active failed", exc_info=True)


# ===========================================================================
# §4.4 Model / Tool / MCP metrics
# ===========================================================================


@lru_cache(maxsize=8)
def _model_calls_total(registry: Any) -> Any:
    return _make_counter(
        "model_calls_total",
        "Total model invocations by model/provider/outcome (observability-and-slo §4.4).",
        ("model", "provider", "outcome"),
        registry,
    )


@lru_cache(maxsize=8)
def _model_call_duration_seconds(registry: Any) -> Any:
    return _make_histogram(
        "model_call_duration_seconds",
        "Model invocation latency in seconds (observability-and-slo §4.4).",
        ("model", "provider"),
        registry,
        _LATENCY_BUCKETS,
    )


@lru_cache(maxsize=8)
def _model_tokens_total(registry: Any) -> Any:
    return _make_counter(
        "model_tokens_total",
        "Total model tokens by model/direction (observability-and-slo §4.4).",
        ("model", "direction"),
        registry,
    )


@lru_cache(maxsize=8)
def _tool_calls_total(registry: Any) -> Any:
    return _make_counter(
        "tool_calls_total",
        "Total tool invocations by registry name (observability-and-slo §4.4). Unknown names normalized to 'other'.",
        ("tool_name",),
        registry,
    )


@lru_cache(maxsize=8)
def _tool_call_duration_seconds(registry: Any) -> Any:
    return _make_histogram(
        "tool_call_duration_seconds",
        "Tool invocation latency in seconds (observability-and-slo §4.4).",
        ("tool_name",),
        registry,
        _LATENCY_BUCKETS,
    )


@lru_cache(maxsize=8)
def _mcp_calls_total(registry: Any) -> Any:
    return _make_counter(
        "mcp_calls_total",
        "Total MCP tool invocations by tool name (observability-and-slo §4.4). Unknown names normalized to 'other'.",
        ("tool_name",),
        registry,
    )


@lru_cache(maxsize=8)
def _mcp_call_duration_seconds(registry: Any) -> Any:
    return _make_histogram(
        "mcp_call_duration_seconds",
        "MCP tool invocation latency in seconds (observability-and-slo §4.4).",
        ("tool_name",),
        registry,
        _LATENCY_BUCKETS,
    )


def normalize_tool_name(name: str | None, known: frozenset[str] | None = None) -> str:
    """Normalize a tool / MCP name per §4.4 ("未知名字归一为 ``other``").

    A controlled catalog can be passed as *known*; names not in it map to
    ``"other"``. When *known* is ``None`` (the default today — no central
    registry yet) every non-empty name passes through, mirroring the spec's
    intent once a catalog exists. Call sites that have an explicit catalog
    (e.g. MCP tools with a fixed server manifest) should pass it.
    """
    if not name:
        return "other"
    if known is not None and name not in known:
        return "other"
    return name


def record_model_call(
    *,
    model: str,
    provider: str,
    outcome: str,
    duration_seconds: float,
    registry: Any = None,
) -> None:
    try:
        labels = {"model": model or "unknown", "provider": provider or "unknown"}
        reg = _registry_or_default(registry)
        _model_calls_total(reg).labels(**_with_constants({**labels, "outcome": outcome})).inc()
        _model_call_duration_seconds(reg).labels(**_with_constants(labels)).observe(duration_seconds)
    except Exception:  # noqa: BLE001
        logger.debug("record_model_call failed", exc_info=True)


def inc_model_tokens(*, model: str, direction: str, tokens: int, registry: Any = None) -> None:
    if tokens <= 0:
        return
    try:
        _model_tokens_total(_registry_or_default(registry)).labels(**_with_constants({"model": model or "unknown", "direction": direction})).inc(tokens)
    except Exception:  # noqa: BLE001
        logger.debug("inc_model_tokens failed", exc_info=True)


def record_tool_call(
    *,
    tool_name: str,
    duration_seconds: float,
    known: frozenset[str] | None = None,
    registry: Any = None,
) -> None:
    try:
        normalized = normalize_tool_name(tool_name, known)
        labels = _with_constants({"tool_name": normalized})
        reg = _registry_or_default(registry)
        _tool_calls_total(reg).labels(**labels).inc()
        _tool_call_duration_seconds(reg).labels(**labels).observe(duration_seconds)
    except Exception:  # noqa: BLE001
        logger.debug("record_tool_call failed", exc_info=True)


def record_mcp_call(
    *,
    tool_name: str,
    duration_seconds: float,
    known: frozenset[str] | None = None,
    registry: Any = None,
) -> None:
    try:
        normalized = normalize_tool_name(tool_name, known)
        labels = _with_constants({"tool_name": normalized})
        reg = _registry_or_default(registry)
        _mcp_calls_total(reg).labels(**labels).inc()
        _mcp_call_duration_seconds(reg).labels(**labels).observe(duration_seconds)
    except Exception:  # noqa: BLE001
        logger.debug("record_mcp_call failed", exc_info=True)


# ===========================================================================
# §4.5 Sandbox metrics
# ===========================================================================


@lru_cache(maxsize=8)
def _sandbox_acquire_duration_seconds(registry: Any) -> Any:
    return _make_histogram(
        "sandbox_acquire_duration_seconds",
        "Sandbox acquisition latency in seconds (observability-and-slo §4.5).",
        (),
        registry,
        _LATENCY_BUCKETS,
    )


@lru_cache(maxsize=8)
def _sandbox_active(registry: Any) -> Any:
    return _make_gauge(
        "sandbox_active",
        "Currently held sandbox leases (observability-and-slo §4.5).",
        (),
        registry,
    )


@lru_cache(maxsize=8)
def _sandbox_pending(registry: Any) -> Any:
    return _make_gauge(
        "sandbox_pending",
        "In-flight sandbox acquisitions waiting for a lease (observability-and-slo §4.5).",
        (),
        registry,
    )


def observe_sandbox_acquire(seconds: float, registry: Any = None) -> None:
    try:
        _sandbox_acquire_duration_seconds(_registry_or_default(registry)).labels(**_with_constants({})).observe(seconds)
    except Exception:  # noqa: BLE001
        logger.debug("observe_sandbox_acquire failed", exc_info=True)


def set_sandbox_active(count: int, registry: Any = None) -> None:
    try:
        _sandbox_active(_registry_or_default(registry)).labels(**_with_constants({})).set(count)
    except Exception:  # noqa: BLE001
        logger.debug("set_sandbox_active failed", exc_info=True)


def set_sandbox_pending(count: int, registry: Any = None) -> None:
    try:
        _sandbox_pending(_registry_or_default(registry)).labels(**_with_constants({})).set(count)
    except Exception:  # noqa: BLE001
        logger.debug("set_sandbox_pending failed", exc_info=True)


# ===========================================================================
# §4.6 DB pool metrics (partial — Redis / Audit outbox / usage / backup deferred)
# ===========================================================================


@lru_cache(maxsize=8)
def _db_pool_in_use(registry: Any) -> Any:
    return _make_gauge(
        "db_pool_in_use",
        "Checked-out SQLAlchemy pool connections (observability-and-slo §4.6). SQLite/memory backends omit.",
        (),
        registry,
    )


@lru_cache(maxsize=8)
def _db_pool_size(registry: Any) -> Any:
    return _make_gauge(
        "db_pool_size",
        "Total SQLAlchemy pool size (checked-in + checked-out) (observability-and-slo §4.6).",
        (),
        registry,
    )


@lru_cache(maxsize=8)
def _db_query_duration_seconds(registry: Any) -> Any:
    return _make_histogram(
        "db_query_duration_seconds",
        "SQL query latency in seconds from SQLAlchemy event listeners (observability-and-slo §4.6).",
        (),
        registry,
        _LATENCY_BUCKETS,
    )


@lru_cache(maxsize=8)
def _db_transaction_failure_total(registry: Any) -> Any:
    return _make_counter(
        "db_transaction_failure_total",
        "Total SQLAlchemy transaction failures by error class (observability-and-slo §4.6).",
        ("error_class",),
        registry,
    )


def set_db_pool_stats(*, in_use: int, size: int, registry: Any = None) -> None:
    try:
        reg = _registry_or_default(registry)
        _db_pool_in_use(reg).labels(**_with_constants({})).set(in_use)
        _db_pool_size(reg).labels(**_with_constants({})).set(size)
    except Exception:  # noqa: BLE001
        logger.debug("set_db_pool_stats failed", exc_info=True)


def observe_db_query(seconds: float, registry: Any = None) -> None:
    try:
        _db_query_duration_seconds(_registry_or_default(registry)).labels(**_with_constants({})).observe(seconds)
    except Exception:  # noqa: BLE001
        logger.debug("observe_db_query failed", exc_info=True)


def inc_db_transaction_failure(*, error_class: str, registry: Any = None) -> None:
    try:
        _db_transaction_failure_total(_registry_or_default(registry)).labels(**_with_constants({"error_class": error_class})).inc()
    except Exception:  # noqa: BLE001
        logger.debug("inc_db_transaction_failure failed", exc_info=True)


# ===========================================================================
# /metrics scrape handler helper
# ===========================================================================


def generate_metrics_payload(registry: Any = None) -> tuple[bytes, str]:
    """Return ``(body_bytes, content_type)`` for the ``/metrics`` scrape endpoint.

    Defaults to the process-global ``REGISTRY`` (where all the lazy singletons
    register when call sites pass ``registry=None`` — the production case).
    Tests pass their own ``CollectorRegistry`` so they can scrape what they
    bumped without polluting the global registry. The content-type is the
    ``prometheus_client`` canonical value so Prometheus accepts it without
    content negotiation.
    """
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    reg = _registry_or_default(registry)
    return generate_latest(reg), CONTENT_TYPE_LATEST


def registry_health() -> bool:
    """Return ``True`` when the prometheus_client registry is importable.

    Used by doctor / readiness checks to surface "metrics layer broken"
    without scraping the full payload.
    """
    try:
        from prometheus_client import REGISTRY  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# §4.6 Audit-outbox metrics (PR-041)
# ===========================================================================
#
# ADR-0005 §14 + observability-and-slo §4.6. These were deferred in the initial
# metrics registry ("blocked on PR-041 outbox") because they have no producer
# until the outbox worker exists. PR-041's worker
# (app.gateway.audit_worker._publish_backlog_metrics) pushes these once per
# drain pass. All are label-less (no org_id — §4.1 cardinality rule), matching
# the ADR's "指标不以 org_id 作为无界公共标签".


@lru_cache(maxsize=8)
def _audit_outbox_pending(registry: Any) -> Any:
    return _make_gauge(
        "audit_outbox_pending",
        "Claimable pending audit-outbox rows (ADR-0005 §14). Backlog over threshold → Class A fail-closed.",
        (),
        registry,
    )


@lru_cache(maxsize=8)
def _audit_outbox_oldest_age_seconds(registry: Any) -> Any:
    return _make_gauge(
        "audit_outbox_oldest_age_seconds",
        "Age in seconds of the oldest claimable pending audit-outbox row (ADR-0005 §14). >5min → P2 alert.",
        (),
        registry,
    )


@lru_cache(maxsize=8)
def _audit_publish_total(registry: Any) -> Any:
    return _make_counter(
        "audit_publish_total",
        "Audit-outbox publish attempts by outcome (ADR-0005 §14).",
        ("outcome",),
        registry,
    )


@lru_cache(maxsize=8)
def _audit_dead_letter_total(registry: Any) -> Any:
    return _make_counter(
        "audit_dead_letter_total",
        "Audit-outbox rows that reached the dead-letter threshold (ADR-0005 §14). >0 → P2 alert.",
        (),
        registry,
    )


@lru_cache(maxsize=8)
def _audit_dead_letter_count(registry: Any) -> Any:
    # Current dead-letter count (gauge) for operators who want the live value
    # rather than the cumulative counter. Distinct name so both scrape.
    return _make_gauge(
        "audit_dead_letter_count",
        "Current dead-letter audit-outbox rows (ADR-0005 §14, live gauge vs the cumulative counter).",
        (),
        registry,
    )


def set_audit_outbox_pending(count: int, registry: Any = None) -> None:
    try:
        _audit_outbox_pending(_registry_or_default(registry)).labels(**_with_constants({})).set(count)
    except Exception:  # noqa: BLE001
        logger.debug("set_audit_outbox_pending failed", exc_info=True)


def set_audit_outbox_oldest_age(seconds: float, registry: Any = None) -> None:
    try:
        _audit_outbox_oldest_age_seconds(_registry_or_default(registry)).labels(**_with_constants({})).set(seconds)
    except Exception:  # noqa: BLE001
        logger.debug("set_audit_outbox_oldest_age failed", exc_info=True)


def inc_audit_publish(*, outcome: str, registry: Any = None) -> None:
    try:
        _audit_publish_total(_registry_or_default(registry)).labels(**_with_constants({"outcome": outcome})).inc()
    except Exception:  # noqa: BLE001
        logger.debug("inc_audit_publish failed", exc_info=True)


def inc_audit_dead_letter(registry: Any = None) -> None:
    try:
        _audit_dead_letter_total(_registry_or_default(registry)).labels(**_with_constants({})).inc()
    except Exception:  # noqa: BLE001
        logger.debug("inc_audit_dead_letter failed", exc_info=True)


def set_audit_dead_letter_count(count: int, registry: Any = None) -> None:
    try:
        _audit_dead_letter_count(_registry_or_default(registry)).labels(**_with_constants({})).set(count)
    except Exception:  # noqa: BLE001
        logger.debug("set_audit_dead_letter_count failed", exc_info=True)


# ===========================================================================
# §4.6 Backup metric (PR-065)
# ===========================================================================
#
# observability-and-slo §4.6 lists ``backup_last_success_timestamp`` as a
# data/audit metric that was deferred ("no infra"). PR-065 introduces the
# application-level backup Job (``scripts/backup.py``); the Job stamps this
# gauge with the backup's ``created_at`` unix timestamp on every successful
# run so an alert (runbook §14.2 "备份超过 RPO 未成功 | P1") can fire when
# ``now - backup_last_success_timestamp > declared_rpo``. Label-less per §4.1
# (backup success is a process-level signal, not per-org).


@lru_cache(maxsize=8)
def _backup_last_success_timestamp(registry: Any) -> Any:
    return _make_gauge(
        "backup_last_success_timestamp",
        "Unix timestamp of the most recent successful application-level backup (PR-065). Alert when now - value > declared RPO (runbook §14.2 P1).",
        (),
        registry,
    )


def set_backup_last_success_timestamp(timestamp: float, registry: Any = None) -> None:
    try:
        _backup_last_success_timestamp(_registry_or_default(registry)).labels(**_with_constants({})).set(timestamp)
    except Exception:  # noqa: BLE001
        logger.debug("set_backup_last_success_timestamp failed", exc_info=True)


# ===========================================================================
# Test-only utilities — NOT for production call sites
# ===========================================================================


def _all_accessor_caches() -> list[Callable]:
    """Return every ``@lru_cache`` accessor in this module (for test teardown)."""
    import sys

    module = sys.modules[__name__]
    return [getattr(module, name) for name in dir(module) if hasattr(getattr(module, name), "cache_clear") and name.startswith("_")]


def reset_accessor_caches_for_tests() -> None:
    """Clear every lazy-singleton cache; tests call this in teardown.

    Production code must not call this — it would orphan the previously
    registered collectors on the global REGISTRY. Tests pass their own
    ``CollectorRegistry`` to the accessors and clear caches between cases so
    a fresh registry gets fresh collector instances.
    """
    for fn in _all_accessor_caches():
        fn.cache_clear()


__all__ = [
    "ALLOWED_LABELS",
    "dec_active_sse_connections",
    "inc_audit_dead_letter",
    "inc_audit_publish",
    "generate_metrics_payload",
    "inc_active_sse_connections",
    "inc_db_transaction_failure",
    "inc_model_tokens",
    "inc_rate_limit",
    "inc_run_cancel",
    "inc_run_reconcile",
    "inc_runs_created",
    "inc_runs_status",
    "normalize_tool_name",
    "observe_db_query",
    "observe_run_admission_duration",
    "observe_run_duration",
    "observe_sandbox_acquire",
    "observe_sse_first_business_event",
    "record_http_request",
    "record_mcp_call",
    "record_model_call",
    "record_tool_call",
    "registry_health",
    "reset_accessor_caches_for_tests",
    "set_db_pool_stats",
    "set_run_reconcile_backlog",
    "set_audit_dead_letter_count",
    "set_audit_outbox_oldest_age",
    "set_audit_outbox_pending",
    "set_sandbox_active",
    "set_sandbox_pending",
    "set_worker_active",
]
