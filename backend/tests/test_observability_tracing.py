"""Tests for ``deerflow.observability.tracing`` (PR-062).

Pins:

* :func:`init_tracing` returns ``None`` (no-op tracer left in place) when no
  exporter is configured, and wires a real provider when one is.
* :func:`get_tracer` always returns a tracer (no-op or real).
* :func:`set_span_attributes` enforces the §5.3 allow-list and drops
  non-allow-listed keys, plus skips ``None`` values.

These tests use the OpenTelemetry in-memory span exporter to capture real
spans rather than mocks, so the SDK wiring (resource attributes, sampler,
exporter) is exercised end-to-end.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from deerflow.config.observability_config import ObservabilityConfig, OtelConfig
from deerflow.observability import tracing


@pytest.fixture
def in_memory_provider(otel_in_memory):
    """Adapter that yields (provider, exporter) for tests that need both.

    The real isolation work happens in the shared ``otel_in_memory`` fixture
    in conftest.py — see its docstring for why OTel's ``set_tracer_provider``
    cannot be used directly for per-test provider swap.
    """
    from opentelemetry import trace

    return trace.get_tracer_provider(), otel_in_memory


# ===========================================================================
# init_tracing — no-op vs wired
# ===========================================================================


class TestInitTracingNoop:
    def test_returns_none_when_endpoint_unset(self):
        cfg = ObservabilityConfig()  # exporter_endpoint=None by default
        assert tracing.init_tracing(cfg) is None

    def test_returns_none_when_endpoint_null_explicit(self):
        cfg = ObservabilityConfig(otel=OtelConfig(exporter_endpoint=None))
        assert tracing.init_tracing(cfg) is None


class TestInitTracingWired:
    def test_returns_shutdown_callable_when_endpoint_set(self):
        # Exercise the real init_tracing wiring without sending spans over the
        # network. OTLPSpanExporter(endpoint=...) only stores the endpoint and
        # connects lazily on export, so constructing it is side-effect-free.
        # init_tracing calls the public ``set_tracer_provider`` which is gated
        # by OTel's Once — but conftest's otel_in_memory reset clears the
        # Once flag, so the call succeeds in this test.
        import opentelemetry.trace as otel_trace

        import deerflow.observability.tracing as tr

        # Pre-reset the Once flag so init_tracing's set_tracer_provider call
        # takes effect (matches the conftest otel_in_memory pattern).
        otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]

        cfg = ObservabilityConfig(
            service_name="test-svc",
            otel=OtelConfig(exporter_endpoint="http://collector.local:4318/v1/traces", sampler_ratio=1.0),
        )
        result = tr.init_tracing(cfg)
        try:
            assert callable(result)
        finally:
            if result is not None:
                result()
            # Restore a clean provider + cleared Once flag for the next test.
            otel_trace._TRACER_PROVIDER = TracerProvider()  # type: ignore[attr-defined]
            otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]


# ===========================================================================
# get_tracer — always returns a tracer
# ===========================================================================


class TestGetTracer:
    def test_returns_a_tracer_even_without_init(self):
        # Before any init, OTel's API returns a no-op proxy tracer. The point
        # is the helper never raises.
        t = tracing.get_tracer("test")
        assert t is not None
        # ``start_as_current_span`` must be callable.
        with t.start_as_current_span("noop"):
            pass


# ===========================================================================
# set_span_attributes — §5.3 allow-list
# ===========================================================================


class TestSetSpanAttributes:
    def test_allow_listed_attributes_set(self, in_memory_provider):
        provider, exporter = in_memory_provider
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("s") as span:
            tracing.set_span_attributes(
                span,
                **{
                    "org_id": "o",
                    "run_id": "r",
                    "thread_id": "t",
                    "release_digest": "d",
                    "policy_version": "v",
                    "route": "/api/x",
                    "model": "gpt",
                    "provider": "openai",
                    "tool_registry_name": "search",
                    "decision": "allow",
                    "error_code": "E1",
                    "http.method": "GET",
                    "http.status_code": 200,
                    "duration_ms": 5,
                },
            )
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes or {}
        assert attrs.get("org_id") == "o"
        assert attrs.get("run_id") == "r"
        assert attrs.get("http.method") == "GET"
        assert attrs.get("http.status_code") == 200
        assert attrs.get("duration_ms") == 5

    def test_non_allow_listed_attribute_dropped(self, in_memory_provider):
        provider, exporter = in_memory_provider
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("s") as span:
            tracing.set_span_attributes(
                span,
                **{
                    "user_email": "leak@example.com",  # not in §5.3 allow-list
                    "raw_prompt": "hello",  # not in allow-list, also a §3.3 forbid
                    "org_id": "ok",
                },
            )
        attrs = exporter.get_finished_spans()[0].attributes or {}
        assert "user_email" not in attrs
        assert "raw_prompt" not in attrs
        assert attrs.get("org_id") == "ok"

    def test_none_values_skipped(self, in_memory_provider):
        provider, exporter = in_memory_provider
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("s") as span:
            tracing.set_span_attributes(
                span,
                **{"org_id": None, "run_id": "r"},
            )
        attrs = exporter.get_finished_spans()[0].attributes or {}
        assert "org_id" not in attrs
        assert attrs.get("run_id") == "r"

    def test_does_not_raise_on_non_recording_span(self):
        # The no-op tracer returns a non-recording span whose set_attribute
        # is a no-op. The helper must not raise in that case.
        noop_tracer = trace.get_tracer("noop-test")
        with noop_tracer.start_as_current_span("n") as span:
            tracing.set_span_attributes(span, org_id="o")  # must not raise
