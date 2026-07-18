"""Logging setup — JSON / text formatters + ``configure_logging`` (PR-062).

Replaces the ad-hoc ``logging.basicConfig`` that previously ran at gateway
import time (``app/gateway/app.py``) with an idempotent setup function that
selects a formatter based on ``observability.log_format`` (§3.1). Both
formatters share the same correlation plumbing so log enrichment is
independent of output shape:

* :class:`JsonFormatter` emits one JSON object per line with the §3.1
  19-field shape (timestamp / level / service / environment /
  deployment_version / message / event_name / request_id / trace_id /
  org_id / workspace_id / principal_type / principal_id / thread_id / run_id /
  release_digest / error_code / duration_ms / outcome) plus scrubbed ``extra``.
* :class:`TextFormatter` keeps today's human-readable
  ``%(asctime)s - %(name)s - %(levelname)s - %(message)s`` shape with an
  appended ``[request_id=… org_id=…]`` correlation suffix; it is the default
  and the regression-safe fallback.

Correlation fields come from :func:`deerflow.observability.correlation.get_correlation`
(the request-bound ``ContextVar``). ``trace_id`` / ``span_id`` are read from
the active OTel span via ``trace.get_current_span()`` so the formatter does
not need to know which middleware opened the span — the correlation context
and the active span agree because the middleware sets both at the same point.
When OTel is uninitialised (no exporter configured) the API layer returns a
non-recording span whose context has an invalid trace id; we treat invalid
trace ids as absent rather than emit ``trace_id="0000000000000000…"``.

§3.3 scrubbing is applied to every ``extra`` key before it reaches the output,
so call sites that accidentally pass a forbidden key are caught at the
choke-point instead of leaking.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Final

from deerflow.config.observability_config import ObservabilityConfig
from deerflow.observability.correlation import get_correlation
from deerflow.observability.scrubbing import scrub_extra

# Names that the JSON formatter lifts from ``record.extra`` (the per-record
# mapping Python attaches when callers pass ``logger.info(..., extra={...})``)
# to top-level fields per §3.1. Everything else stays in the scrubbed ``extra``
# block. ``event_name`` is the join key between log and trace (events.py sets
# the same name as a span attribute).
_LIFTED_EXTRA_FIELDS: Final[tuple[str, ...]] = (
    "event_name",
    "error_code",
    "duration_ms",
    "outcome",
)

# The five OTel attributes the formatter mirrors into the top-level log record
# so a log line carries its own trace identity without a separate lookup.
_TRACE_ID_INVALID: Final[str] = "0" * 32
_SPAN_ID_INVALID: Final[str] = "0" * 16


def _iso_timestamp(record: logging.LogRecord) -> str:
    """Return the record creation time as an ISO 8601 UTC string."""
    dt = datetime.fromtimestamp(record.created, tz=UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _current_span_context() -> tuple[str | None, str | None]:
    """Return ``(trace_id, span_id)`` from the active OTel span, or ``(None, None)``.

    Imports ``opentelemetry.trace`` lazily so this module imports cleanly even
    when the OTel API package is absent (the no-op / text path never calls
    this function and never pays the import cost). An invalid trace id (all
    zeros — the API's sentinel for "no active span") is treated as absent so
    we don't pollute queries with a zero trace id.
    """
    try:
        from opentelemetry.trace import INVALID_SPAN_CONTEXT, get_current_span
    except Exception:  # noqa: BLE001 — OTel API missing or uninitialised
        return None, None
    try:
        ctx = get_current_span().get_span_context()
    except Exception:  # noqa: BLE001 — defensive: any OTel API quirk
        return None, None
    trace_id = f"{ctx.trace_id:032x}"
    span_id = f"{ctx.span_id:016x}"
    if trace_id == _TRACE_ID_INVALID or span_id == _SPAN_ID_INVALID:
        return None, None
    if ctx == INVALID_SPAN_CONTEXT:
        return None, None
    return trace_id, span_id


def _collect_correlation_fields() -> dict[str, Any]:
    """Build the §2 correlation block from the active ``CorrelationContext``.

    Empty / ``None`` values are omitted so a log line never carries
    ``"org_id": null`` — absence reads as "not yet resolved" and keeps the
    JSON shape tight for downstream ingestion that treats ``null`` and
    missing differently.
    """
    ctx = get_correlation()
    if ctx is None:
        return {}
    fields: dict[str, Any] = {
        "request_id": ctx.request_id,
        "trace_id": ctx.trace_id,
        "span_id": ctx.span_id,
        "org_id": ctx.org_id,
        "workspace_id": ctx.workspace_id,
        "principal_type": ctx.principal_type,
        "principal_id": ctx.principal_id,
        "thread_id": ctx.thread_id,
        "run_id": ctx.run_id,
        "release_digest": ctx.release_digest,
        "policy_version": ctx.policy_version,
        "deployment_version": ctx.deployment_version,
        "environment": ctx.environment,
        "service": ctx.service,
    }
    return {key: value for key, value in fields.items() if value}


class JsonFormatter(logging.Formatter):
    """One-JSON-object-per-line formatter per observability-and-slo §3.1.

    Field order matches §3.1 (the spec is explicit about ordering so log
    ingestion can rely on it). Scrubbed ``extra`` is merged under the top
    level after the canonical fields, so callers can attach structured
    detail without going through the canonical-field allow-list.
    """

    def __init__(self, config: ObservabilityConfig) -> None:
        super().__init__()
        self._service = config.service_name
        self._environment = config.environment
        self._deployment_version = config.deployment_version or None

    def format(self, record: logging.LogRecord) -> str:
        # Base §3.1 block in canonical order.
        payload: dict[str, Any] = {
            "timestamp": _iso_timestamp(record),
            "level": record.levelname,
            "service": self._service,
            "environment": self._environment,
            "deployment_version": self._deployment_version,
            "message": record.getMessage(),
            "event_name": None,
        }

        # Per-record extras Python attaches when callers pass ``extra={...}``.
        # We use ``__dict__`` minus the standard LogRecord attribute set so
        # only caller-supplied extras are considered.
        reserved = _RESERVED_LOGRECORD_ATTRS
        raw_extra = {key: value for key, value in record.__dict__.items() if key not in reserved and not key.startswith("_")}
        scrubbed_extra = scrub_extra(raw_extra)

        # Lift §3.1 fields out of extras to the top level.
        for field in _LIFTED_EXTRA_FIELDS:
            if field in scrubbed_extra:
                payload[field] = scrubbed_extra.pop(field)

        # Correlation context (request-scoped). Active OTel span's trace id
        # overrides the correlation context's trace id when present (the
        # middleware sets both to the same value, but the span is the more
        # authoritative source inside nested spans).
        correlation = _collect_correlation_fields()
        trace_id, span_id = _current_span_context()
        if trace_id is not None:
            correlation["trace_id"] = trace_id
        if span_id is not None:
            correlation["span_id"] = span_id
        payload.update({key: value for key, value in correlation.items() if value})

        # Remaining scrubbed extras stay available to the consumer.
        if scrubbed_extra:
            payload.update(scrubbed_extra)

        # Exception / stack info as a string field (mirrors the stdlib default
        # behaviour of appending ``exc_info``).
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        # ``ensure_ascii=False`` keeps non-ASCII content (e.g. Chinese log
        # messages from channels) readable; the line is still valid UTF-8 JSON.
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable formatter preserving today's pre-PR-062 shape.

    Output: ``<asctime> - <name> - <levelname> - <message> [request_id=… org_id=…]``.
    The correlation suffix is omitted entirely when nothing is bound (matching
    today's behaviour for non-request log lines, e.g. startup messages), so
    nothing about plain text logs changes when ``log_format="text"`` and no
    request is in flight.
    """

    _BASE_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self._BASE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        correlation = _collect_correlation_fields()
        trace_id, span_id = _current_span_context()
        if trace_id is not None:
            correlation["trace_id"] = trace_id
        if span_id is not None:
            correlation["span_id"] = span_id
        if not correlation:
            return base
        suffix = " ".join(f"{key}={value}" for key, value in correlation.items())
        return f"{base} [{suffix}]"


# Standard LogRecord attributes that ``__dict__`` always carries; everything
# else is caller-supplied ``extra``. Captured once at import so the formatter
# does not recompute it per record.
_RESERVED_LOGRECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def _make_handler(formatter: logging.Formatter) -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    return handler


def configure_logging(config: ObservabilityConfig) -> None:
    """Install the formatter chosen by ``config.log_format`` on the root logger.

    Idempotent: removes existing handlers we previously installed (tagged via
    ``handler._deerflow_observability = True``) before installing the new one,
    so repeated calls during lifespan reconfiguration do not stack handlers
    (which would duplicate every log line). Third-party handlers (e.g. a
    plugin's FileHandler) are left alone.

    ``apply_logging_level`` (``app_config.py:73``) remains the single source
    of truth for level adjustment; this function only owns handler shape.
    """
    formatter: logging.Formatter
    if config.log_format == "json":
        formatter = JsonFormatter(config)
    else:
        formatter = TextFormatter()

    root = logging.getLogger()
    # Remove only the handlers we previously installed so a third-party
    # plugin handler is not silently dropped.
    for handler in list(root.handlers):
        if getattr(handler, "_deerflow_observability", False):
            root.removeHandler(handler)

    handler = _make_handler(formatter)
    handler._deerflow_observability = True  # type: ignore[attr-defined]
    root.addHandler(handler)


__all__ = ["JsonFormatter", "TextFormatter", "configure_logging"]
