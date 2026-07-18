"""Named observability events — single choke-point for §3.4 events (PR-062).

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

PR-062 ships the helper plus the ``gateway.request.completed`` emit (done by
:class:`CorrelationMiddleware`). The remaining 13 events are emitted by the
PRs that own the relevant code path (``policy.evaluated`` → Track C,
``run.owner.changed`` → ownership PR, ``sandbox.lease.changed`` → Track E,
…) — see runtime-contracts §16.24 for the deferred list.

Contract (mirrors ``tenancy/audit_events.py``):

* this function MUST NOT raise on a logging failure (events are best-effort
  observability, never a correctness gate);
* this function MUST NOT silently no-op — at minimum the event is logged at
  the requested level so none is silently lost.
"""

from __future__ import annotations

import logging
from typing import Any

from deerflow.observability.scrubbing import scrub_extra

# Dedicated logger so event consumers can be filtered / routed independently
# of generic application logging (e.g. ship all ``observability.events`` to a
# dedicated index). The JSON formatter still enriches it with correlation.
_event_logger = logging.getLogger("observability.events")


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
