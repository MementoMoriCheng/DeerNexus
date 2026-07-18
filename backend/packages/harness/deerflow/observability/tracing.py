"""OpenTelemetry tracer lifecycle and span helpers (PR-062).

Implements the §15.1 "OpenTelemetry SDK / Collector 配置" mapping:

* :func:`init_tracing` wires a ``TracerProvider`` + OTLP exporter + head
  sampler when ``observability.otel.exporter_endpoint`` is set, and returns
  ``None`` (leaving the API-layer no-op tracer in place) otherwise. The
  no-op default is what makes PR-062 reversible: an unset endpoint = today's
  behaviour, with zero SDK cost on the hot path.
* :func:`get_tracer` is the single import surface for call sites that need a
  tracer (``correlation_middleware.py``, ``runs/worker.py``).
* :func:`set_span_attributes` enforces the §5.3 allow-list so a call site
  cannot accidentally attach a forbidden attribute (Secret / Prompt /
  high-cardinality raw_url). Anything off the allow-list is dropped silently
  with a DEBUG log — fail-open, because observability must never break the
  request (TEN-008 / fail-closed is for correctness gates; spans are not).

§5.4 sampling is **head-based** in PR-062 (``ParentBased(TraceIdRatioBased)``).
The §5.4 tail-based rule ("errors / Policy deny / Sandbox violations 100%
retained") needs the deny / violation code paths to exist (Track C / Track E);
a follow-up PR will replace the head sampler. The TODO below records the
dependency.

All SDK imports are deferred into :func:`init_tracing` so importing this
module costs nothing when OTel is unconfigured — the no-op path uses only
``opentelemetry.trace`` (the thin API shim), never the SDK.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Final

logger = logging.getLogger(__name__)

# §5.3 allow-list of span attribute names. Anything off this list is dropped
# by ``set_span_attributes`` so a call site cannot smuggle in a forbidden
# value. Mirrors the §5.3 spec verbatim.
_ALLOWED_SPAN_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        # §5.3 explicit allow-list
        "org_id",
        "run_id",
        "thread_id",
        "release_digest",
        "policy_version",
        "route",
        "model",
        "provider",
        "tool_registry_name",
        "decision",
        "error_code",
        # HTTP semantic-convention attributes the middleware attaches. Kept on
        # the allow-list so the middleware does not have to bypass the gate.
        "http.method",
        "http.route",
        "http.status_code",
        "http.response.status_class",
        "http.url",
        "duration_ms",
        "event_name",
    }
)


def get_tracer(name: str) -> Any:
    """Return a tracer for *name*.

    When :func:`init_tracing` has not wired a provider (the default), the
    OpenTelemetry API returns its no-op tracer proxy; ``start_as_current_span``
    on that proxy is a zero-cost context manager that emits no spans. Call
    sites therefore need no conditional — they always go through this helper
    and the cost is paid only when an exporter is configured.
    """
    from opentelemetry.trace import get_tracer as _otel_get_tracer

    return _otel_get_tracer(name)


def set_span_attributes(span: Any, **attrs: Any) -> None:
    """Attach allow-listed attributes to *span* (§5.3).

    Drops any attribute whose key is not on :data:`_ALLOWED_SPAN_ATTRIBUTES`
    (with a DEBUG log naming the dropped key) and any attribute whose value
    is ``None``. Non-recording spans (the no-op default) accept the call
    without effect — their ``set_attribute`` is a no-op, so we don't need a
    special-case branch.
    """
    for key, value in attrs.items():
        if value is None:
            continue
        if key not in _ALLOWED_SPAN_ATTRIBUTES:
            logger.debug(
                "dropping non-allow-listed span attribute key=%s (§5.3 allow-list)",
                key,
            )
            continue
        try:
            span.set_attribute(key, value)
        except Exception:  # noqa: BLE001 — observability must never break the caller
            logger.debug("failed to set span attribute key=%s", key, exc_info=True)


def init_tracing(config: Any) -> Callable[[], None] | None:
    """Initialise the OTel SDK + OTLP exporter when an endpoint is configured.

    Returns a shutdown callable that the gateway lifespan must invoke on
    exit (it flushes the ``BatchSpanProcessor`` so in-flight spans reach the
    collector). Returns ``None`` when no exporter is configured — in that
    case the API-layer no-op tracer stays in place and call sites pay no SDK
    cost.

    Sampling (§5.4): the head sampler is ``ParentBased(TraceIdRatioBased(sampler_ratio))``.
    The tail-based "errors / Policy deny / Sandbox violations 100% retained"
    rule needs the deny / violation code paths (Track C / Track E) and is
    deferred to a follow-up PR — the TODO tag below marks the spot.
    """
    otel = getattr(config, "otel", None)
    endpoint = getattr(otel, "exporter_endpoint", None) if otel is not None else None
    if not endpoint:
        # No exporter configured → API no-op tracer is already in place.
        # Nothing to shut down.
        return None

    sampler_ratio = float(getattr(otel, "sampler_ratio", 0.1))
    service_namespace = getattr(otel, "service_namespace", "deernexus")
    service_name = getattr(config, "service_name", "deer-flow-gateway")
    environment = getattr(config, "environment", "development")
    deployment_version = getattr(config, "deployment_version", "") or None

    # Deferred SDK imports — the no-op path above never reaches here, so a
    # deployment that does not enable OTel never pays the import cost.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    from opentelemetry.trace import set_tracer_provider

    # TODO(PR-062 follow-up): replace TraceIdRatioBased with a tail sampler
    # that retains 100% of errors / Policy deny / Sandbox violations per
    # §5.4. Blocked on the deny / violation code paths landing (Track C /
    # Track E). Until then the head sampler is the documented fallback.
    resource_attributes: dict[str, str] = {
        "service.name": service_name,
        "service.namespace": service_namespace,
        "deployment.environment": environment,
    }
    if deployment_version:
        resource_attributes["service.version"] = deployment_version

    provider = TracerProvider(
        resource=Resource.create(resource_attributes),
        sampler=ParentBased(root=TraceIdRatioBased(rate=sampler_ratio)),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    set_tracer_provider(provider)
    logger.info(
        "OpenTelemetry tracing initialised (endpoint=%s sampler_ratio=%.3f); head-sampling active, tail-based §5.4 rule deferred to a follow-up PR.",
        endpoint,
        sampler_ratio,
    )

    def _shutdown() -> None:
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001 — shutdown is best-effort on the way out
            logger.warning("OpenTelemetry provider shutdown failed", exc_info=True)

    return _shutdown


__all__ = ["get_tracer", "init_tracing", "set_span_attributes"]
