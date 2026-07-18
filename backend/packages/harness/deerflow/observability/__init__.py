"""Observability layer — structured logging, correlation ids, OTel spans (PR-062).

Implements ``docs/ops/observability-and-slo.md`` §2 / §3 / §5 at the
mechanism layer: correlation-id context, JSON / text logging setup, §3.3
forbidden-field scrubbing, OpenTelemetry SDK lifecycle, and the §3.4 named
event sink. HTTP request-path correlation + the HTTP root span are added by
:class:`app.gateway.correlation_middleware.CorrelationMiddleware`; the Run
root span is wrapped around ``run_agent`` in
``deerflow.runtime.runs.worker``.

The namespace is intentionally ``deerflow.observability`` (not
``deerflow.tracing``, which is taken by the Langfuse / LangSmith LLM-tracing
callbacks in ``deerflow.tracing.factory``). The two layers are unrelated:
``deerflow.tracing`` is per-LLM-call callback handlers, this module is
infra-grade observability.

Defaults are today's behaviour (``log_format="text"``, OTel no-op); see
:mod:`deerflow.observability.config` for the reversibility rationale.
"""

from deerflow.config.observability_config import LOG_FORMATS, ObservabilityConfig, OtelConfig
from deerflow.observability.correlation import (
    CorrelationContext,
    bind_correlation,
    get_correlation,
    new_request_id,
    reset_correlation,
    validate_inbound_request_id,
)
from deerflow.observability.events import emit_event
from deerflow.observability.logging_setup import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
)
from deerflow.observability.scrubbing import (
    FORBIDDEN_EXTRA_KEYS,
    looks_forbidden,
    scrub_extra,
)
from deerflow.observability.tracing import get_tracer, init_tracing, set_span_attributes

__all__ = [
    "FORBIDDEN_EXTRA_KEYS",
    "CorrelationContext",
    "JsonFormatter",
    "LOG_FORMATS",
    "ObservabilityConfig",
    "OtelConfig",
    "TextFormatter",
    "bind_correlation",
    "configure_logging",
    "emit_event",
    "get_correlation",
    "get_tracer",
    "init_tracing",
    "looks_forbidden",
    "new_request_id",
    "reset_correlation",
    "scrub_extra",
    "set_span_attributes",
    "validate_inbound_request_id",
]
