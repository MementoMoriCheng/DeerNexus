"""Tests for ``deerflow.observability.scrubbing`` (PR-062).

Pins the §3.3 forbidden-field behaviour: every §3.3 bullet must be caught by
``looks_forbidden`` case-insensitively, and ``scrub_extra`` must replace the
value with ``"<redacted>"`` while preserving non-forbidden keys. This is the
single gate that prevents secrets / prompts from reaching the JSON formatter.
"""

from __future__ import annotations

import pytest

from deerflow.observability.scrubbing import (
    FORBIDDEN_EXTRA_KEYS,
    looks_forbidden,
    scrub_extra,
)


class TestLooksForbidden:
    @pytest.mark.parametrize(
        "key",
        [
            "authorization",
            "Authorization",
            "AUTHORIZATION",
            "cookie",
            "api_key",
            "apikey",
            "secret",
            "token",
            "dsn",
            "password",
            "passwd",
            "prompt",
            "response",
            "claims",
            "file_body",
            "signed_url",
        ],
    )
    def test_forbidden_substring_matched_case_insensitively(self, key: str):
        assert looks_forbidden(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            # Prefixed / suffixed keys still match because the forbidden word
            # appears as a full token element after splitting on non-alphanumerics.
            "httpx_authorization",
            "sqlalchemy_password",
            "x_api_key",
            "bearer_token",
            "user_secret_id",
        ],
    )
    def test_prefixed_keys_also_matched(self, key: str):
        assert looks_forbidden(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "request_id",
            "org_id",
            "run_id",
            "method",
            "route",
            "model",
            "duration_ms",
            "outcome",
            "status_code",
            # Plurals / counts of forbidden concepts are benign — token-aware
            # matching must NOT redact these (``tokens`` is a count, not a
            # secret; ``responses`` is a list of status objects, not a body).
            "tokens",
            "responses",
            "secrets_count",  # 'secrets' != 'secret'
            "tokenization_ms",
        ],
    )
    def test_benign_keys_not_matched(self, key: str):
        assert looks_forbidden(key) is False

    @pytest.mark.parametrize(
        "key",
        [
            # These DO match because the forbidden word is a full token element
            # even though the key as a whole is compound. The operator who needs
            # a benign variant must rename it (e.g. ``status_code`` not
            # ``response_status``).
            "response_status",
            "prompt_template_name",
            "token_value",
        ],
    )
    def test_compound_keys_with_full_forbidden_token_matched(self, key: str):
        assert looks_forbidden(key) is True

    def test_non_string_key_is_safe(self):
        # Non-string keys cannot enable substring attacks; treat as non-forbidden
        # so the formatter can still serialise them.
        assert looks_forbidden(123) is False  # type: ignore[arg-type]

    def test_empty_string_is_not_forbidden(self):
        assert looks_forbidden("") is False


class TestScrubExtra:
    def test_none_returns_empty_dict(self):
        assert scrub_extra(None) == {}

    def test_empty_mapping_returns_empty_dict(self):
        assert scrub_extra({}) == {}

    def test_forbidden_value_replaced_with_redacted_placeholder(self):
        out = scrub_extra({"authorization": "Bearer abc123"})
        assert out == {"authorization": "<redacted>"}

    def test_benign_keys_pass_through_unchanged(self):
        out = scrub_extra({"request_id": "abc", "org_id": "org-1", "method": "GET"})
        assert out == {"request_id": "abc", "org_id": "org-1", "method": "GET"}

    def test_mixed_forbidden_and_benign(self):
        out = scrub_extra(
            {
                "request_id": "abc",
                "token": "secret-value",
                "model": "gpt-test",
                "cookie": "session=xyz",
            }
        )
        assert out["request_id"] == "abc"
        assert out["model"] == "gpt-test"
        assert out["token"] == "<redacted>"
        assert out["cookie"] == "<redacted>"

    def test_forbidden_key_is_preserved_redacted_not_dropped(self):
        # The reader of a log line should see that *something* was there and
        # the scrubber intervened — silent drop hides regressions.
        out = scrub_extra({"secret": "hunter2"})
        assert "secret" in out
        assert out["secret"] == "<redacted>"

    def test_non_string_values_pass_through(self):
        # Numbers / lists / dicts are not secrets by shape; preserve them so
        # structured extras (durations, lists of routes) survive.
        out = scrub_extra({"duration_ms": 42, "tags": ["a", "b"], "nested": {"k": 1}})
        assert out == {"duration_ms": 42, "tags": ["a", "b"], "nested": {"k": 1}}

    def test_does_not_mutate_input(self):
        original = {"token": "x", "ok": "y"}
        snapshot = dict(original)
        scrub_extra(original)
        assert original == snapshot


class TestForbiddenKeysCoverSpec:
    def test_registry_covers_all_section_3_3_bullets(self):
        # observability-and-slo §3.3 forbidden fields, lower-cased. Every bullet
        # must have at least one entry in FORBIDDEN_EXTRA_KEYS.
        spec_bullets = [
            "authorization",  # Authorization / API Key
            "cookie",
            "api_key",
            "secret",  # Secret / Token / DSN
            "token",
            "dsn",
            "prompt",  # full Prompt / Response
            "response",
            "file_body",  # 文件正文
            "signed_url",  # 签名 URL query
            "claims",  # OIDC 完整 claims
        ]
        registry_lower = " ".join(FORBIDDEN_EXTRA_KEYS)
        for bullet in spec_bullets:
            assert bullet in registry_lower, f"§3.3 bullet {bullet!r} missing from FORBIDDEN_EXTRA_KEYS"
