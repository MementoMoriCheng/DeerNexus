"""Tests for ``deerflow.observability.events.emit_event`` (PR-062).

Pins the §3.4 named-event sink: correlation injection from the active
context, §3.3 scrubbing of caller-supplied fields, the ``event_name`` field
lifted to the top level (so it is the log↔trace join key), the active-span
``event_name`` attribute, the default level, and the never-raises contract
(mirrors ``tenancy/audit_events.emit_tenant_event``).
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from opentelemetry import trace

from deerflow.observability import emit_event
from deerflow.observability.correlation import (
    CorrelationContext,
    bind_correlation,
    reset_correlation,
)
from deerflow.observability.logging_setup import JsonFormatter


@pytest.fixture
def event_buffer():
    """Wire observability.events logger to an in-memory JSON buffer."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter(__import__("deerflow.config.observability_config", fromlist=["ObservabilityConfig"]).ObservabilityConfig(log_format="json")))
    logger = logging.getLogger("observability.events")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield logger, buf
    logger.handlers.clear()


@pytest.fixture
def in_memory_provider(otel_in_memory):
    """Adapter yielding the exporter from the shared conftest otel_in_memory fixture."""
    return otel_in_memory


def _records(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


# ===========================================================================
# Basic emit shape
# ===========================================================================


class TestEmitShape:
    def test_emits_named_event(self, event_buffer):
        _, buf = event_buffer
        emit_event("gateway.request.completed", message="done")
        rec = _records(buf)[0]
        assert rec["event_name"] == "gateway.request.completed"
        assert rec["message"] == "done"

    def test_message_defaults_to_event_name(self, event_buffer):
        _, buf = event_buffer
        emit_event("run.created")
        rec = _records(buf)[0]
        assert rec["message"] == "run.created"

    def test_default_level_is_info(self, event_buffer):
        _, buf = event_buffer
        emit_event("x")
        assert _records(buf)[0]["level"] == "INFO"

    def test_custom_level_respected(self, event_buffer):
        _, buf = event_buffer
        emit_event("x", level=logging.WARNING)
        assert _records(buf)[0]["level"] == "WARNING"


# ===========================================================================
# Correlation injection
# ===========================================================================


class TestCorrelationInjection:
    def test_request_id_injected_from_active_context(self, event_buffer):
        _, buf = event_buffer
        token = bind_correlation(CorrelationContext(request_id="req-1", org_id="org-1", run_id="run-1"))
        try:
            emit_event("run.created")
        finally:
            reset_correlation(token)
        rec = _records(buf)[0]
        assert rec["request_id"] == "req-1"
        assert rec["org_id"] == "org-1"
        assert rec["run_id"] == "run-1"

    def test_no_correlation_means_no_correlation_fields(self, event_buffer):
        _, buf = event_buffer
        emit_event("x")
        rec = _records(buf)[0]
        for absent in ("request_id", "org_id", "run_id"):
            assert absent not in rec


# ===========================================================================
# §3.3 scrubbing
# ===========================================================================


class TestScrubbing:
    def test_forbidden_field_redacted(self, event_buffer):
        _, buf = event_buffer
        emit_event("x", token="SECRET-VALUE")
        rec = _records(buf)[0]
        assert rec["token"] == "<redacted>"
        assert "SECRET-VALUE" not in buf.getvalue()

    def test_benign_field_preserved(self, event_buffer):
        _, buf = event_buffer
        emit_event("x", model="gpt-x", tokens=5)
        rec = _records(buf)[0]
        assert rec["model"] == "gpt-x"
        assert rec["tokens"] == 5

    def test_lifted_fields_go_to_top_level(self, event_buffer):
        _, buf = event_buffer
        emit_event("x", outcome="2xx", duration_ms=12, error_code="E1")
        rec = _records(buf)[0]
        assert rec["outcome"] == "2xx"
        assert rec["duration_ms"] == 12
        assert rec["error_code"] == "E1"


# ===========================================================================
# Active span attribute — log ↔ trace join
# ===========================================================================


class TestSpanAttribute:
    def test_event_name_set_on_active_span(self, in_memory_provider, event_buffer):
        exporter = in_memory_provider
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("outer"):
            emit_event("tool.call.completed", model="gpt-x")
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes or {}
        assert attrs.get("event_name") == "tool.call.completed"
        # model is on the §5.3 allow-list so it lands on the span too.
        assert attrs.get("model") == "gpt-x"

    def test_no_active_span_does_not_raise(self, event_buffer):
        # Outside any span context — emit_event must still log and not blow up.
        _, buf = event_buffer
        emit_event("audit.outbox.result")
        assert _records(buf)[0]["event_name"] == "audit.outbox.result"


# ===========================================================================
# Never-raises contract
# ===========================================================================


class TestNeverRaises:
    def test_does_not_raise_when_logger_fails(self, monkeypatch, event_buffer):
        logger, _ = event_buffer

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(logger, "log", boom)
        # Must not raise — best-effort observability, never a correctness gate.
        emit_event("x")

    def test_does_not_raise_when_otel_fails(self, monkeypatch, event_buffer):
        def boom():
            raise RuntimeError("otel broken")

        monkeypatch.setattr("opentelemetry.trace.get_current_span", boom)
        emit_event("x")  # must not raise
