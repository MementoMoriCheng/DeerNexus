"""Prometheus scrape endpoint (PR-063).

Exposes ``GET <metrics.route>`` (default ``/metrics``) returning the
``prometheus_client`` text exposition format. Public (no auth) by design —
``observability-and-slo.md`` §4.1 forbids high-cardinality id labels on public
metrics, so the payload carries no sensitive data. Deployments that want to
restrict scrapes should use an ingress rule.

The route is gated on ``observability.metrics.enabled``: when disabled it is
not registered, so the path 404s rather than returning an empty payload.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from deerflow.observability.metrics import generate_metrics_payload

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics_endpoint() -> Response:
    """Return the Prometheus text exposition format for the global registry."""
    body, content_type = generate_metrics_payload()
    return Response(content=body, media_type=content_type)


__all__ = ["router"]
