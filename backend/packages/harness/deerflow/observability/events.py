"""Named observability events — single choke-point for §3.4 events (PR-062 / PR-063).

``observability-and-slo.md`` §3.4 names 14 stable event identifiers
(``gateway.request.completed``, ``run.created``, ``run.status.changed``,
``run.owner.changed``, ``run.reconcile.result``, ``policy.evaluated``,
``tool.call.completed``, ``mcp.call.completed``, ``sandbox.lease.changed``,
``model.call.completed``, ``release.resolved``, ``audit.outbox.result``, …).
Every call site should go through :func:`emit_event` rather than ad-hoc
``logger.info`` so that:

* correlation ids (``request_id`` / ``trace_id`` / ``org_id`` / ``run_id`` /
  …) are attached from the active :class:`CorrelationContext` in one place;
* the event name lands both on the log record (top-level ``event_name`` field
  via the JSON formatter) and on the active span (as the ``event_name``
  attribute), making the log↔trace join trivial;
* §3.3 scrubbing is applied uniformly — a call site that accidentally passes
  ``token=…`` is caught at the choke-point;
* the sink can be swapped in a later PR (outbox write, metrics counter bump,
  audit bus) without touching call sites, mirroring
  ``deerflow.tenancy.audit_events.emit_tenant_event``.

PR-063 adds an ``event_name → counter`` fan-out (:data:`_EVENT_METRIC_FANOUT`)
so a §3.4 event also drives the matching §4 metric increment without the call
site knowing about prometheus. Today only the events PR-062/063 wire are
mapped; future PRs that emit a §3.4 event get the metric bump for free once
they add their event name here.

Contract (mirrors ``tenancy/audit_events.py``):

* this function MUST NOT raise on a logging failure (events are best-effort
  observability, never a correctness gate);
* this function MUST NOT silently no-op — at minimum the event is logged at
  the requested level so none is silently lost.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from deerflow.observability.scrubbing import scrub_extra

# Dedicated logger so event consumers can be filtered / routed independently
# of generic application logging (e.g. ship all ``observability.events`` to a
# dedicated index). The JSON formatter still enriches it with correlation.
_event_logger = logging.getLogger("observability.events")


# ---------------------------------------------------------------------------
# §3.4 event → §4 metric fan-out (PR-063)
# ---------------------------------------------------------------------------
#
# Maps a §3.4 event name to a callable that bumps the matching §4 metric,
# given the event's scrubbed fields. A future PR that emits ``run.created``
# via emit_event gets ``runs_created_total`` incremented for free once its
# name is added here. Unmapped event names incur no metric bump (the log /
# span side still fires) — this is intentional so an event without a natural
# counter (e.g. ``tenant.context.bound``) doesn't force one.
#
# The callable takes the scrubbed ``fields`` dict and is wrapped in try/except
# by ``emit_event`` so a registry error can never break the event path.


def _fanout_gateway_request_completed(fields: dict[str, Any]) -> None:
    """Bump §4.2 http_requests_total + http_request_duration_seconds.

    The CorrelationMiddleware is the sole emitter of
    ``gateway.request.completed`` today and passes the structured labels
    (method / route_template / outcome=status_class / error_code /
    duration_ms) as event fields. This fan-out reads them and drives the
    counters, so the middleware does NOT call ``record_http_request``
    directly — that would double-count. Future callers of
    ``emit_event("gateway.request.completed", ...)`` get the counter bump
    for free as long as they pass the same fields.
    """
    from deerflow.observability import metrics

    metrics.record_http_request(
        method=fields.get("method", "") or "",
        route_template=fields.get("route_template", "") or "",
        status_class=fields.get("outcome", "") or "",
        error_code=fields.get("error_code"),
        duration_seconds=float(fields.get("duration_ms", 0) or 0) / 1000.0,
    )


_EVENT_METRIC_FANOUT: dict[str, Callable[[dict[str, Any]], None]] = {
    "gateway.request.completed": _fanout_gateway_request_completed,
}


def emit_event(
    event_name: str,
    *,
    level: int = logging.INFO,
    message: str | None = None,
    **fields: Any,
) -> None:
    """Emit a named observability event (§3.4).

    Args:
        event_name: Stable event identifier from §3.4 (e.g.
            ``"gateway.request.completed"``). Lands as the top-level
            ``event_name`` log field and as an ``event_name`` span attribute.
            Drives the matching §4 metric increment via
            :data:`_EVENT_METRIC_FANOUT` when mapped.
        level: stdlib logging level (default INFO). §3.2 level guidance
            applies — do not log expected Policy deny / 404 as ERROR.
        message: Human-readable message. Defaults to ``event_name``.
        **fields: Structured event details. §3.3 forbidden keys are
            scrubbed. Lifted to top-level fields when the JSON formatter
            recognises them (``error_code``, ``duration_ms``, ``outcome``);
            the rest merge into the log record.

    The correlation context (request_id / org_id / run_id / …) is attached
    automatically from the active :class:`CorrelationContext` — callers do
    not pass them. The active span receives ``event_name`` as an attribute
    so a trace query for the event finds the span.

    Never raises: a logging or OTel failure is contained (best-effort
    observability, never a correctness gate).
    """
    text = message if message else event_name
    scrubbed = scrub_extra(fields)
    extra: dict[str, Any] = {"event_name": event_name, **scrubbed}

    # Attach the event name to the active span (if any) so the trace side
    # records the same event. ``set_span_attributes`` enforces the §5.3
    # allow-list; ``event_name`` is on it. The no-op tracer makes this a
    # zero-cost call when no span is active.
    try:
        from opentelemetry.trace import get_current_span

        span = get_current_span()
        # Cheaply skip the non-recording case so we don't even build the
        # attribute payload on the hot path.
        if span is not None and hasattr(span, "is_recording") and span.is_recording():
            span.set_attribute("event_name", event_name)
            for key, value in scrubbed.items():
                if value is None:
                    continue
                # Only §5.3 allow-listed keys make it onto the span; the
                # rest are log-only. ``set_span_attributes`` drops silently.
                from deerflow.observability.tracing import set_span_attributes

                set_span_attributes(span, **{key: value})
    except Exception:  # noqa: BLE001 — observability must never raise
        pass

    # §3.4 → §4 metric fan-out (PR-063). The handler is wrapped in try/except
    # so a registry error (e.g. label cardinality bug) never breaks the log /
    # span side. Unmapped event names skip this (no natural counter).
    fanout = _EVENT_METRIC_FANOUT.get(event_name)
    if fanout is not None:
        try:
            fanout(scrubbed)
        except Exception:  # noqa: BLE001 — best-effort; never break the caller
            pass

    # ``correlation`` is read by the formatter via ``get_correlation()`` — we
    # do not duplicate it into ``extra`` to avoid the formatter seeing it
    # twice. The ``extra=`` kwarg is reserved for caller-supplied structured
    # fields (after scrubbing) plus the lifted ``event_name``.
    try:
        _event_logger.log(level, text, extra=extra)
    except Exception:  # noqa: BLE001 — best-effort; never break the caller
        # Swallow quietly — we cannot log about a logging failure without
        # risking recursion. The correlation context is unchanged.
        pass


__all__ = ["emit_event"]
