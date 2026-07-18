"""Correlation middleware — request_id + HTTP root span + log event (PR-062).

Outermost middleware (added last in ``create_app()`` so it runs first on the
request path). Owns three things:

1. The per-request **correlation id** (``X-Request-Id``). Inbound header is
   validated (§2 anti-log-injection) and honoured when well-formed; otherwise
   a fresh id is generated. The id is exposed to downstream middleware via
   ``request.state.request_id`` (read by
   :class:`TenantResolutionMiddleware`) and to all log records via the
   ``CorrelationContext`` ContextVar.
2. The **HTTP root span** (§5.1 ``HTTP <method> <route_template>``). The
   span's trace id becomes the request's ``trace_id``, joining log and trace
   for the request. When OTel is uninitialised (no exporter configured) the
   span is a zero-cost no-op.
3. The **``gateway.request.completed`` event** (§3.4 first entry) emitted at
   request close with status class / duration / outcome — the only §3.4
   event PR-062 wires; the rest land with their owning PRs.

Fail-open contract: observability is never a correctness gate. A failure in
correlation binding or event emission must not break the request —
:class:`TenantResolutionMiddleware` remains the fail-closed gate and is
unaffected. The ContextVar ``try/finally`` is still paired so a bind does not
leak across task reuse even when the request itself errors.

Span / handler ordering (re cap on ``BaseHTTPMiddleware`` running in reverse
add-order): registered last → outermost → runs before Auth / Tenant /
CSRF / CORS, so its trace context is the parent of every inner span and its
correlation id is visible to every downstream middleware.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from deerflow.observability import (
    CorrelationContext,
    bind_correlation,
    emit_event,
    get_tracer,
    new_request_id,
    reset_correlation,
    set_span_attributes,
    validate_inbound_request_id,
)

logger = logging.getLogger(__name__)

_REQUEST_ID_HEADER = "X-Request-Id"

# Re-exported as the response header so clients can correlate their request
# with the server's logs / traces. Always the server-resolved id (generated
# or validated-inbound), never the raw unvalidated inbound header.
_RESPONSE_REQUEST_ID_HEADER = "X-Request-Id"

# Tracer name used for the HTTP root span. Resolved per-request (see
# ``dispatch``) rather than cached at module import: ``ProxyTracer._tracer``
# caches the underlying SDK tracer on first use, so a module-level cache would
# pin the tracer to whatever provider was active at first request and never
# pick up a later provider swap (e.g. tests, or a future hot-reload of OTel
# config). Per-request ``get_tracer`` is a cheap dict lookup on the provider.
_TRACER_NAME = "deerflow.gateway.http"


def _resolve_route_template(request: Request) -> str:
    """Return the route template (e.g. ``/api/threads/{thread_id}``) for the span name.

    Falls back to the raw path when Starlette has not matched a route yet
    (e.g. 404) or when ``path_format`` is unavailable — §5.1 allows the path
    as the fallback identifier. We avoid logging the raw query string.
    """
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None)
    if path_format:
        return path_format
    return request.url.path


def _status_class(status_code: int) -> str:
    """Return the HTTP status class label (``2xx`` / ``4xx`` / ``5xx`` / …)."""
    if status_code < 100 or status_code >= 600:
        return "unknown"
    return f"{status_code // 100}xx"


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Bind correlation id + open HTTP root span + emit completion event."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 1. Resolve request_id (validate inbound or generate).
        inbound = request.headers.get(_REQUEST_ID_HEADER)
        request_id = validate_inbound_request_id(inbound) or new_request_id()
        request.state.request_id = request_id

        # 2. Open the HTTP root span (§5.1). At outermost-middleware time the
        # router has not matched yet, so we open the span with the raw path
        # and re-resolve + rename it after ``call_next`` returns (when
        # ``scope["route"]`` is populated). ``start_as_current_span`` makes
        # this the active span so the formatter / events helper pick up its
        # trace id automatically; no-op when OTel is uninitialised.
        initial_route = _resolve_route_template(request)
        span_name = f"HTTP {request.method} {initial_route}"
        start_monotonic = time.perf_counter()

        tracer = get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span(span_name) as span:
            set_span_attributes(
                span,
                **{
                    "http.method": request.method,
                    "http.url": str(request.url),
                },
            )

            # 3. Bind the correlation context. We populate only the fields
            # known at the edge; downstream middleware / handlers enrich via
            # a re-bind (e.g. tenant resolver sets org_id / principal_*).
            deployment_version = _read_deployment_version()
            correlation = CorrelationContext(
                request_id=request_id,
                deployment_version=deployment_version or None,
                environment=_read_environment(),
                service=_read_service_name(),
            )
            token = bind_correlation(correlation)

            status_code = 500
            error_code: str | None = None
            try:
                response = await call_next(request)
                status_code = response.status_code
                # Echo the resolved request id so clients can correlate.
                response.headers[_RESPONSE_REQUEST_ID_HEADER] = request_id
                return response
            except Exception:
                # ``call_next`` raising means an inner middleware / handler
                # blew up outside Starlette's exception machinery. The OTel
                # ``with`` context manager records the exception on the span
                # automatically when we re-raise; status_code stays at 500
                # for the completion event emitted in ``finally``.
                logger.exception(
                    "unhandled exception in call_next (request_id=%s)",
                    request_id,
                )
                raise
            finally:
                duration_ms = int((time.perf_counter() - start_monotonic) * 1000)
                # Post-routing: scope now carries the matched route, so we can
                # set ``http.route`` and rename the span to the §5.1 form.
                route_template = _resolve_route_template(request)
                try:
                    span.update_name(f"HTTP {request.method} {route_template}")
                except Exception:  # noqa: BLE001 — non-recording span / OTel absent
                    pass
                set_span_attributes(
                    span,
                    **{
                        "http.route": route_template,
                        "http.status_code": status_code,
                        "http.response.status_class": _status_class(status_code),
                        "duration_ms": duration_ms,
                        "error_code": error_code,
                    },
                )
                # §3.4 first event — only one PR-062 wires. Level mapping
                # follows §3.2: 5xx = ERROR, 4xx = INFO (expected; §3.2
                # forbids logging ordinary 4xx as ERROR), else INFO.
                level = logging.ERROR if status_code >= 500 else logging.INFO
                emit_event(
                    "gateway.request.completed",
                    level=level,
                    message=f"HTTP {request.method} {route_template} → {status_code}",
                    duration_ms=duration_ms,
                    outcome=_status_class(status_code),
                    error_code=error_code,
                )
                reset_correlation(token)


# ---------------------------------------------------------------------------
# Config accessors — deferred so this module imports without a loaded
# config.yaml (matches the tenancy / production doctor pattern). Each accessor
# swallows config-read failure so the middleware is fail-open even when the
# observability config block is absent.
# ---------------------------------------------------------------------------


def _read_observability_config() -> object | None:
    try:
        from deerflow.config import get_app_config

        return get_app_config().observability
    except Exception:  # noqa: BLE001 — config not loaded; middleware still works
        return None


def _read_service_name() -> str | None:
    cfg = _read_observability_config()
    return getattr(cfg, "service_name", None) if cfg is not None else None


def _read_environment() -> str | None:
    cfg = _read_observability_config()
    return getattr(cfg, "environment", None) if cfg is not None else None


def _read_deployment_version() -> str:
    cfg = _read_observability_config()
    return getattr(cfg, "deployment_version", "") or ""


__all__ = ["CorrelationMiddleware"]
