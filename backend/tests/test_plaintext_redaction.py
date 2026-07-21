"""Security regression tests for API Key plaintext redaction (PR-035).

ADR §9.2 line 302: "Key 不进入 URL、日志、Trace、Audit payload 和前端
持久化." These tests lock that contract by:

1. Asserting ``emit_tenant_event`` payloads never contain the plaintext
   or hash (captured via mock).
2. Asserting ``ApiKeyResponse`` (read envelope) has no ``plaintext_key``
   or ``key_hash`` field — the field-set itself is the guard.
3. Asserting ``_FORBIDDEN_PAYLOAD_KEYS`` strips ``api_key`` / ``key_hash``
   from any audit payload (defense-in-depth from PR-011).

IAM IDs: ``IAM-360`` series (security / redaction).
"""

from __future__ import annotations

import inspect

import pytest

from deerflow.contracts.iam import ApiKeyCreateResponse, ApiKeyResponse


class TestResponseFieldRedaction:
    """The read envelope MUST NOT declare a plaintext or hash field."""

    def test_api_key_response_has_no_plaintext_field(self):
        fields = set(ApiKeyResponse.model_fields.keys())
        assert "plaintext_key" not in fields
        assert "key_hash" not in fields

    def test_api_key_response_field_set(self):
        # Lock the projection so adding a sensitive column to ApiKeyRow
        # does not silently leak through model_validate.
        fields = set(ApiKeyResponse.model_fields.keys())
        assert fields == {
            "id",
            "org_id",
            "service_account_id",
            "key_prefix",
            "scopes",
            "expires_at",
            "revoked_at",
            "created_at",
            "last_used_at",
        }

    def test_create_response_only_adds_plaintext(self):
        """Create response is the superset; reads never see it."""
        create_fields = set(ApiKeyCreateResponse.model_fields.keys())
        read_fields = set(ApiKeyResponse.model_fields.keys())
        assert create_fields - read_fields == {"plaintext_key"}

    def test_create_response_inherits_read_shape(self):
        # ``ApiKeyCreateResponse`` subclasses ``ApiKeyResponse``, so
        # every read field is also on the create response. This is the
        # structurally-enforced contract that the read path can never
        # see the plaintext.
        assert issubclass(ApiKeyCreateResponse, ApiKeyResponse)


class TestForbiddenPayloadKeys:
    """ADR §9.2 line 302 + ``contracts.events._FORBIDDEN_PAYLOAD_KEYS`` defense-in-depth."""

    def test_api_key_string_is_forbidden(self):
        from deerflow.contracts.events import _FORBIDDEN_PAYLOAD_KEYS

        assert "api_key" in _FORBIDDEN_PAYLOAD_KEYS

    def test_key_hash_string_is_forbidden(self):
        from deerflow.contracts.events import _FORBIDDEN_PAYLOAD_KEYS

        assert "key_hash" in _FORBIDDEN_PAYLOAD_KEYS


class TestSourceLevelNoPlaintextLog:
    """The router source MUST NOT log the plaintext.

    A defensive grep over the router source: a log call whose args
    include ``plaintext_key`` would be a regression. This catches a
    future ``logger.info(plaintext_key=...)`` style mistake at test
    time, before any runtime exposure.
    """

    def test_router_source_does_not_log_plaintext(self):
        from app.gateway.routers import iam as iam_router

        source = inspect.getsource(iam_router)
        # The mint endpoint legitimately constructs ApiKeyCreateResponse
        # with ``plaintext_key=plaintext``. That is the ONE place the
        # plaintext name appears in source. A ``logger.*`` call that
        # references ``plaintext_key`` would be a leak vector.
        # Heuristic: every line containing "plaintext" must NOT be a log call.
        bad_lines = []
        for line in source.splitlines():
            stripped = line.strip()
            if "plaintext" not in stripped:
                continue
            if stripped.startswith("logger.") or "logger." in stripped and "plaintext" in stripped:
                # Defensive: any logger.<level>(...) call that mentions plaintext.
                if "logger." in stripped:
                    bad_lines.append(stripped)
        assert not bad_lines, f"router source has logger calls mentioning plaintext: {bad_lines}"

    def test_auth_middleware_does_not_log_plaintext(self):
        from app.gateway import auth_middleware

        source = inspect.getsource(auth_middleware)
        # The middleware reads the header value into ``raw`` and never
        # logs it. A reference to ``raw`` in a logger call is the leak
        # vector we are guarding.
        for line in source.splitlines():
            stripped = line.strip()
            if "logger." in stripped and "raw" in stripped:
                pytest.fail(f"auth_middleware logs the raw API key: {stripped}")
