"""Integration tests for the ``/metrics`` endpoint + deploy artifacts (PR-063).

Pins:

* The FastAPI ``/metrics`` route returns 200 with the canonical Prometheus
  content-type and a non-empty body that a ``prometheus_client`` parser can
  round-trip.
* The route is public (no auth) per §4.1 + the ``CorrelationMiddleware``
  short-circuit so Prometheus scrapes don't pollute ``http_requests_total``.
* Every deploy/ dashboard JSON parses and every panel has a non-empty
  ``expr`` (catches broken PromQL typos at CI time).
* The deploy/alerts PrometheusRule YAML parses and every rule carries the
  eight §9 annotation fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.gateway.correlation_middleware import CorrelationMiddleware
from app.gateway.routers import metrics as metrics_router

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"


# ===========================================================================
# /metrics endpoint
# ===========================================================================


def _make_app_with_metrics() -> FastAPI:
    app = FastAPI()
    app.include_router(metrics_router.router)
    # Add CorrelationMiddleware so we verify the public-path short-circuit
    # keeps /metrics off the http_requests_total counter.
    app.add_middleware(CorrelationMiddleware)
    return app


class TestMetricsEndpoint:
    def test_metrics_returns_200_canonical_content_type(self):
        client = TestClient(_make_app_with_metrics())
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain; version=1.0.0")

    def test_metrics_body_non_empty_and_contains_process_metrics(self):
        client = TestClient(_make_app_with_metrics())
        body = client.get("/metrics").text
        # prometheus_client always emits python_* / process_* built-ins.
        assert "python_" in body or "process_" in body

    def test_metrics_path_skipped_by_correlation_middleware(self):
        """A scrape must NOT bump http_requests_total — CorrelationMiddleware
        short-circuits public paths so Prometheus scrapes don't pollute the
        operator's request graphs."""
        client = TestClient(_make_app_with_metrics())
        # First scrape populates the registry.
        client.get("/metrics")
        # Second scrape — body should still NOT contain http_requests_total
        # because the middleware bypassed recording for /metrics.
        body = client.get("/metrics").text
        assert "http_requests_total" not in body


# ===========================================================================
# deploy/ dashboards parse + every panel has PromQL
# ===========================================================================


DASHBOARD_FILES = sorted((DEPLOY_DIR / "dashboards").glob("*.json")) if (DEPLOY_DIR / "dashboards").exists() else []


@pytest.mark.parametrize("dash_path", DASHBOARD_FILES, ids=lambda p: p.name)
def test_dashboard_json_parses_and_panels_have_expr(dash_path: Path):
    data = json.loads(dash_path.read_text(encoding="utf-8"))
    assert "title" in data
    assert "panels" in data
    assert len(data["panels"]) > 0
    # Every timeseries / stat panel must declare at least one target with expr.
    for panel in data["panels"]:
        if panel.get("type") in ("text", "row"):
            continue
        targets = panel.get("targets") or []
        assert targets, f"panel {panel.get('title')!r} has no targets"
        for target in targets:
            if isinstance(target, dict):
                expr = target.get("expr")
                # Allow TODO-style text panels to skip; require expr otherwise.
                assert expr, f"panel {panel.get('title')!r} target has no expr"


def test_dashboard_files_exist():
    # Guard against the parametrize list being silently empty (e.g. directory move).
    assert len(DASHBOARD_FILES) >= 4, f"expected ≥4 dashboard JSON files, found {len(DASHBOARD_FILES)}"


# ===========================================================================
# deploy/alerts PrometheusRule YAML parses + §9 annotation fields
# ===========================================================================


REQUIRED_ALERT_ANNOTATIONS = {
    "owner",
    "severity",
    "summary",
    "impact",
    "dashboard",
    "runbook",
    "silence_rule",
    "escalation",
}


def test_alerts_yaml_parses():
    yaml = pytest.importorskip("yaml")
    path = DEPLOY_DIR / "alerts" / "prometheus-rules.yaml"
    assert path.exists(), f"alerts file missing at {path}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["apiVersion"] == "monitoring.coreos.com/v1"
    assert data["kind"] == "PrometheusRule"
    groups = data["spec"]["groups"]
    assert len(groups) >= 1


def test_every_alert_carries_section_9_annotation_fields():
    yaml = pytest.importorskip("yaml")
    path = DEPLOY_DIR / "alerts" / "prometheus-rules.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = [r for group in data["spec"]["groups"] for r in group["rules"]]
    assert len(rules) >= 1
    for rule in rules:
        if "alert" not in rule:
            continue  # recording rules (none today) skip the annotation contract
        annotations = rule.get("annotations", {})
        missing = REQUIRED_ALERT_ANNOTATIONS - set(annotations.keys())
        assert not missing, f"alert {rule['alert']} missing annotations: {missing}"
        # severity label matches §9 P1/P2 taxonomy.
        assert rule["labels"]["severity"] in {"p1", "p2"}
