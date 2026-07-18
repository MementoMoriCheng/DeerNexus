"""Tests for ``deerflow.observability.logging_setup`` (PR-062).

Pins the §3.1 JSON output shape (canonical 19 fields, correct ordering),
correlation injection from the active :class:`CorrelationContext`, trace id
injection from the active OTel span, §3.3 scrubbing of forbidden extras, and
the text fallback that preserves today's pre-PR-062 output shape.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from deerflow.config.observability_config import ObservabilityConfig
from deerflow.observability.correlation import (
    CorrelationContext,
    bind_correlation,
    reset_correlation,
)
from deerflow.observability.logging_setup import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
)


@pytest.fixture
def json_logger():
    """Wire a JsonFormatter to an in-memory buffer; yield (logger, buffer)."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter(ObservabilityConfig(log_format="json", deployment_version="v1.2.3")))
    logger = logging.getLogger("test.observability.json")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield logger, buf
    logger.handlers.clear()


def _records(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


# ===========================================================================
# §3.1 JSON shape
# ===========================================================================


class TestJsonShape:
    def test_emits_one_json_object_per_line(self, json_logger):
        logger, buf = json_logger
        logger.info("hello")
        logger.info("world")
        records = _records(buf)
        assert len(records) == 2
        assert records[0]["message"] == "hello"
        assert records[1]["message"] == "world"

    def test_canonical_fields_present(self, json_logger):
        logger, buf = json_logger
        logger.info("msg")
        record = _records(buf)[0]
        # Every §3.1 field must exist (None when unset is fine).
        for field in (
            "timestamp",
            "level",
            "service",
            "environment",
            "deployment_version",
            "message",
            "event_name",
        ):
            assert field in record, f"missing §3.1 field {field!r}"

    def test_level_is_recorded(self, json_logger):
        logger, buf = json_logger
        logger.warning("warn")
        assert _records(buf)[0]["level"] == "WARNING"

    def test_service_and_environment_from_config(self, json_logger):
        logger, buf = json_logger
        logger.info("m")
        rec = _records(buf)[0]
        assert rec["service"] == "deer-flow-gateway"
        assert rec["environment"] == "development"

    def test_deployment_version_present_when_configured(self, json_logger):
        logger, buf = json_logger
        logger.info("m")
        assert _records(buf)[0]["deployment_version"] == "v1.2.3"

    def test_deployment_version_omitted_when_empty(self):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JsonFormatter(ObservabilityConfig()))  # empty version
        logger = logging.getLogger("test.observability.json.noversion")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.info("m")
        logger.handlers.clear()
        rec = _records(buf)[0]
        # None / empty suppressed: the field is absent or null, not a placeholder.
        assert rec.get("deployment_version") in (None, "")
        assert rec["deployment_version"] != "unknown"
        assert rec["deployment_version"] != "unset"

    def test_timestamp_is_iso8601_utc(self, json_logger):
        logger, buf = json_logger
        logger.info("m")
        ts = _records(buf)[0]["timestamp"]
        assert ts.endswith("Z")
        # parses as RFC 3339
        from datetime import datetime

        datetime.fromisoformat(ts.replace("Z", "+00:00"))

    def test_event_name_lifted_from_extra_to_top_level(self, json_logger):
        logger, buf = json_logger
        logger.info("hi", extra={"event_name": "gateway.request.completed"})
        assert _records(buf)[0]["event_name"] == "gateway.request.completed"

    def test_outcome_duration_error_code_lifted_to_top_level(self, json_logger):
        logger, buf = json_logger
        logger.info(
            "done",
            extra={"outcome": "2xx", "duration_ms": 12, "error_code": None},
        )
        rec = _records(buf)[0]
        assert rec["outcome"] == "2xx"
        assert rec["duration_ms"] == 12
        # error_code=None still lifted (so the field is always present).
        assert "error_code" in rec

    def test_non_lifted_extras_remain_under_top_level(self, json_logger):
        logger, buf = json_logger
        logger.info("m", extra={"model": "gpt-x", "tokens": 5})
        rec = _records(buf)[0]
        assert rec["model"] == "gpt-x"
        assert rec["tokens"] == 5

    def test_exception_info_serialised(self, json_logger):
        logger, buf = json_logger
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("captured")
        rec = _records(buf)[0]
        assert "exception" in rec
        assert "ValueError" in rec["exception"]
        assert "boom" in rec["exception"]


# ===========================================================================
# Correlation injection
# ===========================================================================


class TestCorrelationInjection:
    def test_request_id_injected_from_active_context(self, json_logger):
        logger, buf = json_logger
        token = bind_correlation(CorrelationContext(request_id="req-abc", org_id="org-1"))
        try:
            logger.info("m")
        finally:
            reset_correlation(token)
        rec = _records(buf)[0]
        assert rec["request_id"] == "req-abc"
        assert rec["org_id"] == "org-1"

    def test_all_correlation_fields_injected(self, json_logger):
        logger, buf = json_logger
        ctx = CorrelationContext(
            request_id="r",
            org_id="o",
            workspace_id="w",
            principal_type="user",
            principal_id="p",
            thread_id="t",
            run_id="u",
            release_digest="d",
            policy_version="v",
        )
        token = bind_correlation(ctx)
        try:
            logger.info("m")
        finally:
            reset_correlation(token)
        rec = _records(buf)[0]
        for field, expected in (
            ("request_id", "r"),
            ("org_id", "o"),
            ("workspace_id", "w"),
            ("principal_type", "user"),
            ("principal_id", "p"),
            ("thread_id", "t"),
            ("run_id", "u"),
            ("release_digest", "d"),
            ("policy_version", "v"),
        ):
            assert rec[field] == expected, f"{field} not injected"

    def test_unset_correlation_fields_omitted(self, json_logger):
        logger, buf = json_logger
        # Only request_id set; every other field should be absent (not null).
        token = bind_correlation(CorrelationContext(request_id="r"))
        try:
            logger.info("m")
        finally:
            reset_correlation(token)
        rec = _records(buf)[0]
        assert rec["request_id"] == "r"
        # Absent fields do not appear at all.
        assert "org_id" not in rec
        assert "run_id" not in rec

    def test_no_correlation_means_no_correlation_fields(self, json_logger):
        logger, buf = json_logger
        logger.info("m")
        rec = _records(buf)[0]
        for absent in ("request_id", "org_id", "run_id", "trace_id"):
            assert absent not in rec


# ===========================================================================
# §3.3 scrubbing in the formatter choke-point
# ===========================================================================


class TestScrubbingInFormatter:
    def test_forbidden_extra_redacted(self, json_logger):
        logger, buf = json_logger
        logger.info("m", extra={"authorization": "Bearer SECRET"})
        rec = _records(buf)[0]
        assert rec["authorization"] == "<redacted>"
        assert "SECRET" not in buf.getvalue()

    def test_benign_extra_preserved(self, json_logger):
        logger, buf = json_logger
        logger.info("m", extra={"model": "gpt-x", "request_id_client": "abc"})
        rec = _records(buf)[0]
        assert rec["model"] == "gpt-x"


# ===========================================================================
# TextFormatter — preserves today's behaviour
# ===========================================================================


class TestTextFormatter:
    def test_emits_human_readable_line_without_correlation(self):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(TextFormatter())
        logger = logging.getLogger("test.observability.text.plain")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.info("hello")
        logger.handlers.clear()
        line = buf.getvalue().strip()
        # Today's shape: asctime - name - level - message
        assert "INFO" in line
        assert "hello" in line
        # No correlation suffix when nothing is bound.
        assert "[" not in line

    def test_correlation_suffix_appended_when_bound(self):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(TextFormatter())
        logger = logging.getLogger("test.observability.text.bound")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        token = bind_correlation(CorrelationContext(request_id="r1", org_id="o1"))
        try:
            logger.info("m")
        finally:
            reset_correlation(token)
        logger.handlers.clear()
        line = buf.getvalue().strip()
        assert "request_id=r1" in line
        assert "org_id=o1" in line


# ===========================================================================
# configure_logging — idempotent handler replacement
# ===========================================================================


class TestConfigureLogging:
    def test_text_default_installs_text_handler(self):
        root = logging.getLogger()
        before = list(root.handlers)
        try:
            configure_logging(ObservabilityConfig(log_format="text"))
            # Exactly one of our handlers is installed; pre-existing third-party
            # handlers are untouched.
            ours = [h for h in root.handlers if getattr(h, "_deerflow_observability", False)]
            assert len(ours) == 1
            assert isinstance(ours[0].formatter, TextFormatter)
        finally:
            # restore
            for h in list(root.handlers):
                if getattr(h, "_deerflow_observability", False):
                    root.removeHandler(h)
            for h in before:
                if h not in root.handlers:
                    root.addHandler(h)

    def test_json_format_swaps_formatter(self):
        root = logging.getLogger()
        before = list(root.handlers)
        try:
            configure_logging(ObservabilityConfig(log_format="text"))
            configure_logging(ObservabilityConfig(log_format="json"))
            ours = [h for h in root.handlers if getattr(h, "_deerflow_observability", False)]
            assert len(ours) == 1
            assert isinstance(ours[0].formatter, JsonFormatter)
        finally:
            for h in list(root.handlers):
                if getattr(h, "_deerflow_observability", False):
                    root.removeHandler(h)
            for h in before:
                if h not in root.handlers:
                    root.addHandler(h)

    def test_repeated_calls_do_not_stack_handlers(self):
        root = logging.getLogger()
        before = list(root.handlers)
        try:
            for _ in range(5):
                configure_logging(ObservabilityConfig(log_format="json"))
            ours = [h for h in root.handlers if getattr(h, "_deerflow_observability", False)]
            assert len(ours) == 1  # never stacks
        finally:
            for h in list(root.handlers):
                if getattr(h, "_deerflow_observability", False):
                    root.removeHandler(h)
            for h in before:
                if h not in root.handlers:
                    root.addHandler(h)
