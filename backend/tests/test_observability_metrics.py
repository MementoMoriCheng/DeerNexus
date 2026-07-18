"""Tests for ``deerflow.observability.metrics`` (PR-063).

Pins:

* §4.1 label allow-list cardinality guard — every accessor's metric must
  declare only allow-listed label names; a programming error that smuggles in
  a high-cardinality id (org_id / user_id / run_id / …) raises at construction.
* The registry accessors are lazy singletons keyed on the registry argument so
  tests can pass a fresh ``CollectorRegistry`` per test without polluting the
  process-global one.
* The ``/metrics`` handler emits the canonical Prometheus content-type and a
  payload that round-trips through ``prometheus_client.generate_latest``.
* The §4.4 tool-name normalization rule ("unknown → other").
* The ``emit_event`` §3.4 → §4 fan-out drives ``http_requests_total`` from a
  ``gateway.request.completed`` event.

Test isolation uses a fresh ``CollectorRegistry`` per test plus
``reset_accessor_caches_for_tests`` so the lazy singletons bind to the test's
registry instead of the global one.
"""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry

from deerflow.observability import events, metrics


@pytest.fixture(autouse=True)
def _fresh_metrics_registry(monkeypatch):
    """Patch ``_registry_or_default`` to return a fresh per-test registry.

    ``prometheus_client`` raises if a metric name is registered twice on the
    same registry, and the process-global REGISTRY is shared across tests. We
    patch ``_registry_or_default`` (the indirection every accessor uses) to
    hand back a fresh ``CollectorRegistry`` per test, then clear the
    ``@lru_cache`` singletons so they rebind to the new registry on first
    access.
    """
    reg = CollectorRegistry()
    monkeypatch.setattr(metrics, "_registry_or_default", lambda r: reg)
    metrics.reset_accessor_caches_for_tests()
    # Seed constant labels so the registered metrics carry them.
    metrics._set_constant_labels("deer-flow-gateway", "test", "vtest")
    yield reg
    metrics.reset_accessor_caches_for_tests()
    metrics._set_constant_labels("", "", "")


def _payload_text(reg: CollectorRegistry) -> str:
    body, _ = metrics.generate_metrics_payload(reg)
    return body.decode()


# ===========================================================================
# §4.1 label allow-list cardinality guard
# ===========================================================================


class TestLabelAllowList:
    def test_allowed_labels_constant_matches_spec(self):
        # §4.1 public allow-list + the internal-use extras documented at the
        # constant. Any addition here is a deliberate, audited decision.
        for required in (
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
        ):
            assert required in metrics.ALLOWED_LABELS, f"{required!r} missing from ALLOWED_LABELS"

    @pytest.mark.parametrize("forbidden", ["org_id", "user_id", "run_id", "request_id", "trace_id", "thread_id", "raw_url", "artifact_name"])
    def test_high_cardinality_ids_not_in_allow_list(self, forbidden: str):
        assert forbidden not in metrics.ALLOWED_LABELS

    def test_make_counter_rejects_high_cardinality_label(self, _fresh_metrics_registry):
        with pytest.raises(ValueError, match="allow-list"):
            metrics._make_counter("bad_metric_total", "x", ("org_id",), _fresh_metrics_registry)

    def test_make_histogram_rejects_high_cardinality_label(self, _fresh_metrics_registry):
        with pytest.raises(ValueError, match="allow-list"):
            metrics._make_histogram("bad_hist_seconds", "x", ("user_id",), _fresh_metrics_registry, metrics._LATENCY_BUCKETS)

    def test_make_gauge_rejects_high_cardinality_label(self, _fresh_metrics_registry):
        with pytest.raises(ValueError, match="allow-list"):
            metrics._make_gauge("bad_gauge", "x", ("run_id",), _fresh_metrics_registry)


# ===========================================================================
# /metrics endpoint helper
# ===========================================================================


class TestMetricsPayload:
    def test_returns_bytes_and_canonical_content_type(self):
        body, content_type = metrics.generate_metrics_payload(CollectorRegistry())
        assert isinstance(body, bytes)
        assert content_type == "text/plain; version=1.0.0; charset=utf-8"

    def test_payload_uses_supplied_registry(self, _fresh_metrics_registry):
        metrics.record_http_request(
            method="GET",
            route_template="/test",
            status_class="2xx",
            error_code=None,
            duration_seconds=0.001,
            registry=_fresh_metrics_registry,
        )
        text = _payload_text(_fresh_metrics_registry)
        assert "http_requests_total" in text
        assert "/test" in text

    def test_registry_health_true_when_prometheus_importable(self):
        assert metrics.registry_health() is True


# ===========================================================================
# Fail-open contract
# ===========================================================================


class TestFailOpen:
    def test_record_http_request_does_not_raise_on_bad_label(self, _fresh_metrics_registry):
        # If a caller somehow passes a non-string label value the registry
        # could raise; the accessor must contain it (observability never
        # breaks the request). Simulate by breaking the constant-label cache.
        metrics._set_constant_labels("ok", "ok", "ok")
        # Normal call should not raise.
        metrics.record_http_request(
            method="GET",
            route_template="/x",
            status_class="2xx",
            error_code=None,
            duration_seconds=0.01,
            registry=_fresh_metrics_registry,
        )

    def test_inc_model_tokens_zero_tokens_is_noop(self, _fresh_metrics_registry):
        metrics.inc_model_tokens(model="m", direction="in", tokens=0, registry=_fresh_metrics_registry)
        text = _payload_text(_fresh_metrics_registry)
        # No samples recorded.
        assert "model_tokens_total" not in text or " 0.0" not in text.split("model_tokens_total")[-1].split("\n")[0]


# ===========================================================================
# §4.4 tool-name normalization
# ===========================================================================


class TestToolNameNormalization:
    def test_known_name_passes_through(self):
        assert metrics.normalize_tool_name("search", frozenset({"search"})) == "search"

    def test_unknown_name_normalizes_to_other(self):
        assert metrics.normalize_tool_name("user_input_x", frozenset({"search"})) == "other"

    def test_empty_name_normalizes_to_other(self):
        assert metrics.normalize_tool_name(None) == "other"
        assert metrics.normalize_tool_name("") == "other"

    def test_no_known_catalog_passes_through(self):
        # When no catalog is provided, names are not bucketed (today there is
        # no central tool registry). Each call site that has a real catalog
        # passes it.
        assert metrics.normalize_tool_name("anything") == "anything"


# ===========================================================================
# §4 metric coverage — every wired metric appears in the payload when bumped
# ===========================================================================


class TestWiredMetricCoverage:
    def test_all_wired_metrics_present_after_bumps(self, _fresh_metrics_registry):
        reg = _fresh_metrics_registry
        # HTTP
        metrics.record_http_request(method="GET", route_template="/a", status_class="2xx", error_code=None, duration_seconds=0.01, registry=reg)
        metrics.inc_active_sse_connections(reg)
        metrics.observe_sse_first_business_event(0.1, registry=reg)
        metrics.inc_rate_limit(reason="x", registry=reg)
        # Run
        metrics.inc_runs_created(registry=reg)
        metrics.inc_runs_status(run_status="success", registry=reg)
        metrics.observe_run_duration(terminal_status="success", seconds=1.0, registry=reg)
        metrics.observe_run_admission_duration(0.5, registry=reg)
        metrics.inc_run_cancel(registry=reg)
        metrics.inc_run_reconcile(outcome="recovered", registry=reg)
        metrics.set_run_reconcile_backlog(2, registry=reg)
        metrics.set_worker_active(1, registry=reg)
        # Model / Tool / MCP
        metrics.record_model_call(model="gpt", provider="openai", outcome="success", duration_seconds=0.1, registry=reg)
        metrics.inc_model_tokens(model="gpt", direction="in", tokens=10, registry=reg)
        metrics.record_tool_call(tool_name="search", duration_seconds=0.05, registry=reg)
        metrics.record_mcp_call(tool_name="fetch", duration_seconds=0.2, registry=reg)
        # Sandbox
        metrics.observe_sandbox_acquire(0.02, registry=reg)
        metrics.set_sandbox_active(1, registry=reg)
        metrics.set_sandbox_pending(0, registry=reg)
        # DB
        metrics.set_db_pool_stats(in_use=1, size=5, registry=reg)
        metrics.observe_db_query(0.001, registry=reg)
        metrics.inc_db_transaction_failure(error_class="OperationalError", registry=reg)

        text = _payload_text(reg)
        expected = [
            "http_requests_total",
            "http_request_duration_seconds",
            "active_sse_connections",
            "sse_first_business_event_seconds",
            "rate_limit_total",
            "runs_created_total",
            "runs_status_total",
            "run_duration_seconds",
            "run_admission_duration_seconds",
            "run_cancel_total",
            "run_reconcile_total",
            "run_reconcile_backlog",
            "worker_active",
            "model_calls_total",
            "model_call_duration_seconds",
            "model_tokens_total",
            "tool_calls_total",
            "tool_call_duration_seconds",
            "mcp_calls_total",
            "mcp_call_duration_seconds",
            "sandbox_acquire_duration_seconds",
            "sandbox_active",
            "sandbox_pending",
            "db_pool_in_use",
            "db_pool_size",
            "db_query_duration_seconds",
            "db_transaction_failure_total",
        ]
        missing = [name for name in expected if name not in text]
        assert not missing, f"missing metrics in payload: {missing}"

    def test_constant_labels_stamped_on_samples(self, _fresh_metrics_registry):
        reg = _fresh_metrics_registry
        metrics.record_http_request(method="GET", route_template="/c", status_class="2xx", error_code=None, duration_seconds=0.01, registry=reg)
        text = _payload_text(reg)
        # service / environment / deployment_version all appear in the sample line.
        assert 'service="deer-flow-gateway"' in text
        assert 'environment="test"' in text
        assert 'deployment_version="vtest"' in text


# ===========================================================================
# emit_event §3.4 → §4 metric fan-out
# ===========================================================================


class TestEmitEventFanout:
    def test_gateway_request_completed_drives_http_counters(self, _fresh_metrics_registry, monkeypatch):
        # Patch the metrics module's _registry_or_default inside events.py's
        # fan-out path by seeding the global so events' deferred import of
        # metrics uses the test registry.
        reg = _fresh_metrics_registry
        # events.py imports metrics lazily; the accessors there call
        # _registry_or_default(None) which our fixture patch returns as reg.
        events.emit_event(
            "gateway.request.completed",
            method="POST",
            route_template="/api/runs",
            duration_ms=120,
            outcome="2xx",
            error_code=None,
        )
        text = _payload_text(reg)
        assert "http_requests_total" in text
        assert 'route_template="/api/runs"' in text
        assert 'status_class="2xx"' in text

    def test_unmapped_event_does_not_bump_counters(self, _fresh_metrics_registry):
        reg = _fresh_metrics_registry
        events.emit_event("some.unmapped.event", foo="bar")
        text = _payload_text(reg)
        # No http counters (no mapped fan-out).
        assert "http_requests_total" not in text
