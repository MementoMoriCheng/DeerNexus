"""Tests for ``app.gateway.correlation_middleware.CorrelationMiddleware`` (PR-062).

Pins the three things the middleware owns:

1. **Request id resolution** — honours a valid inbound ``X-Request-Id``,
   generates a fresh id when absent, and rejects an invalid inbound header
   (§2 anti-log-injection) by generating a fresh one instead of trusting
   the client value.
2. **HTTP root span** (§5.1) — opens ``HTTP <method> <route_template>``
   and closes it, with the http.* semantic attributes attached.
3. **``gateway.request.completed`` event** (§3.4 first entry) — emitted on
   every request close with the correct level mapping (5xx=ERROR, else INFO),
   duration_ms and outcome.

Uses an in-memory OTel exporter to capture real spans, and an in-memory
log buffer to capture the named event, so the wiring is exercised
end-to-end rather than via mocks.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.gateway.correlation_middleware import CorrelationMiddleware
from deerflow.observability.logging_setup import JsonFormatter


@pytest.fixture
def in_memory_provider(otel_in_memory):
    """Adapter yielding the exporter from the shared conftest otel_in_memory fixture."""
    return otel_in_memory


@pytest.fixture
def event_buffer():
    """Wire the named-event logger to an in-memory JSON buffer."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    from deerflow.config.observability_config import ObservabilityConfig

    handler.setFormatter(JsonFormatter(ObservabilityConfig(log_format="json")))
    logger = logging.getLogger("observability.events")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield buf
    logger.handlers.clear()


def _events(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip() and "gateway.request.completed" in line]


def _make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/items/{item_id}")
    async def get_item(item_id: int):
        return {"item_id": item_id}

    @app.get("/api/boom")
    async def boom():
        raise RuntimeError("kaboom")

    @app.get("/api/teapot")
    async def teapot():
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=418, content={"err": "teapot"})

    app.add_middleware(CorrelationMiddleware)
    return app


# ===========================================================================
# Request id resolution
# ===========================================================================


class TestRequestIdResolution:
    def test_generates_id_when_no_header(self, in_memory_provider, event_buffer):
        client = TestClient(_make_app())
        resp = client.get("/api/items/1")
        assert resp.status_code == 200
        echoed = resp.headers.get("X-Request-Id")
        assert echoed is not None
        assert len(echoed) == 32  # uuid4().hex
        # The echoed id lands on the gateway.request.completed event.
        ev = _events(event_buffer)[0]
        assert ev["request_id"] == echoed

    def test_honours_valid_inbound_header(self, in_memory_provider, event_buffer):
        client = TestClient(_make_app())
        resp = client.get("/api/items/1", headers={"X-Request-Id": "client-req-abc-123"})
        assert resp.status_code == 200
        assert resp.headers["X-Request-Id"] == "client-req-abc-123"
        ev = _events(event_buffer)[0]
        assert ev["request_id"] == "client-req-abc-123"

    def test_rejects_invalid_inbound_header_and_generates_fresh(self, in_memory_provider, event_buffer):
        # Contains a newline — would enable log injection per §2.
        malicious = "abc\nFAKE"
        client = TestClient(_make_app())
        resp = client.get("/api/items/1", headers={"X-Request-Id": malicious})
        assert resp.status_code == 200
        echoed = resp.headers["X-Request-Id"]
        assert echoed != malicious
        assert "\n" not in echoed
        assert "FAKE" not in echoed
        assert len(echoed) == 32  # fresh hex uuid

    def test_rejects_oversized_inbound_header(self, in_memory_provider, event_buffer):
        oversized = "a" * 200
        client = TestClient(_make_app())
        resp = client.get("/api/items/1", headers={"X-Request-Id": oversized})
        assert resp.status_code == 200
        echoed = resp.headers["X-Request-Id"]
        assert echoed != oversized
        assert len(echoed) == 32

    def test_trims_whitespace_from_valid_header(self, in_memory_provider, event_buffer):
        client = TestClient(_make_app())
        resp = client.get("/api/items/1", headers={"X-Request-Id": "  client-id-1  "})
        assert resp.headers["X-Request-Id"] == "client-id-1"


# ===========================================================================
# HTTP root span (§5.1)
# ===========================================================================


class TestHttpRootSpan:
    def test_span_name_uses_method_and_route_template(self, in_memory_provider, event_buffer):
        exporter = in_memory_provider
        client = TestClient(_make_app())
        client.get("/api/items/42")
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "HTTP GET /api/items/{item_id}"

    def test_http_attributes_set_on_span(self, in_memory_provider, event_buffer):
        exporter = in_memory_provider
        client = TestClient(_make_app())
        client.get("/api/items/42")
        attrs = exporter.get_finished_spans()[0].attributes or {}
        assert attrs.get("http.method") == "GET"
        assert attrs.get("http.route") == "/api/items/{item_id}"
        assert attrs.get("http.status_code") == 200
        assert attrs.get("http.response.status_class") == "2xx"
        assert "duration_ms" in attrs

    def test_status_class_correct_for_4xx(self, in_memory_provider, event_buffer):
        exporter = in_memory_provider
        client = TestClient(_make_app())
        client.get("/api/teapot")  # 418
        attrs = exporter.get_finished_spans()[0].attributes or {}
        assert attrs.get("http.status_code") == 418
        assert attrs.get("http.response.status_class") == "4xx"


# ===========================================================================
# gateway.request.completed event (§3.4 first entry)
# ===========================================================================


class TestRequestCompletedEvent:
    def test_event_emitted_on_2xx_with_info_level(self, in_memory_provider, event_buffer):
        client = TestClient(_make_app())
        client.get("/api/items/1")
        evs = _events(event_buffer)
        assert len(evs) == 1
        assert evs[0]["event_name"] == "gateway.request.completed"
        assert evs[0]["level"] == "INFO"
        assert evs[0]["outcome"] == "2xx"
        assert "duration_ms" in evs[0]

    def test_event_emitted_on_4xx_with_info_level_not_error(self, in_memory_provider, event_buffer):
        # §3.2 forbids logging ordinary 4xx as ERROR.
        client = TestClient(_make_app())
        client.get("/api/teapot")
        ev = _events(event_buffer)[0]
        assert ev["level"] == "INFO"
        assert ev["outcome"] == "4xx"

    def test_event_emitted_on_5xx_with_error_level(self, in_memory_provider, event_buffer):
        client = TestClient(_make_app())
        with __import__("pytest").raises(Exception):  # the route raises; TestClient surfaces it
            client.get("/api/boom")
        ev = _events(event_buffer)[0]
        assert ev["level"] == "ERROR"
        assert ev["outcome"] == "5xx"

    def test_message_includes_method_route_and_status(self, in_memory_provider, event_buffer):
        client = TestClient(_make_app())
        client.get("/api/items/1")
        ev = _events(event_buffer)[0]
        assert "GET" in ev["message"]
        assert "/api/items/{item_id}" in ev["message"]
        assert "200" in ev["message"]


# ===========================================================================
# Fail-open contract
# ===========================================================================


class TestFailOpen:
    def test_request_succeeds_even_when_event_logger_misbehaves(self, in_memory_provider, monkeypatch):
        # If the named-event logger blows up, the request must still complete.
        logger = logging.getLogger("observability.events")

        def boom(self, *args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(logger, "log", boom)
        client = TestClient(_make_app())
        resp = client.get("/api/items/1")
        # 200 — observability failure must not break the request.
        assert resp.status_code == 200
