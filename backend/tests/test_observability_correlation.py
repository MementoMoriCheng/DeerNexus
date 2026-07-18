"""Tests for ``deerflow.observability.correlation`` (PR-062).

Pins the §2 correlation-id ContextVar lifecycle (bind/reset/get, mirroring
``contracts/context.py``'s tenant pattern) and the inbound ``X-Request-Id``
validation that enforces §2's anti-log-injection rule (length 1–128, only
``[A-Za-z0-9._-]``).
"""

from __future__ import annotations

import asyncio

import pytest

from deerflow.observability.correlation import (
    CorrelationContext,
    bind_correlation,
    get_correlation,
    new_request_id,
    reset_correlation,
    validate_inbound_request_id,
)

# ===========================================================================
# ContextVar lifecycle
# ===========================================================================


class TestContextVarLifecycle:
    def test_get_returns_none_when_unbound(self):
        assert get_correlation() is None

    def test_bind_then_get_returns_bound_context(self):
        ctx = CorrelationContext(request_id="req-1", org_id="org-1")
        token = bind_correlation(ctx)
        try:
            assert get_correlation() is ctx
        finally:
            reset_correlation(token)
        assert get_correlation() is None

    def test_reset_restores_previous_value(self):
        outer = CorrelationContext(request_id="outer")
        inner = CorrelationContext(request_id="inner")
        outer_token = bind_correlation(outer)
        try:
            inner_token = bind_correlation(inner)
            assert get_correlation() is inner
            reset_correlation(inner_token)
            # After reset, the outer is visible again (not None).
            assert get_correlation() is outer
        finally:
            reset_correlation(outer_token)
        assert get_correlation() is None

    def test_reset_in_finally_restores_on_exception(self):
        ctx = CorrelationContext(request_id="req-x")
        token = bind_correlation(ctx)
        with pytest.raises(RuntimeError, match="boom"):
            try:
                raise RuntimeError("boom")
            finally:
                reset_correlation(token)
        assert get_correlation() is None

    def test_correlation_context_is_frozen(self):
        ctx = CorrelationContext(request_id="req-1")
        with pytest.raises(Exception):  # FrozenInstanceError
            ctx.request_id = "mutated"  # type: ignore[misc]

    def test_request_id_is_required_other_fields_optional(self):
        # Every §2 field except request_id is optional.
        ctx = CorrelationContext(request_id="req-1")
        assert ctx.org_id is None
        assert ctx.trace_id is None
        assert ctx.run_id is None
        assert ctx.release_digest is None


# ===========================================================================
# asyncio task inheritance (mirrors contracts/context.py semantics)
# ===========================================================================


class TestAsyncioInheritance:
    def test_correlation_inherited_by_create_task(self):
        """``asyncio.create_task`` copies the parent context — the run worker
        relies on this so its log lines carry the request's correlation."""
        ctx = CorrelationContext(request_id="parent-req", org_id="org-1")
        seen: dict[str, str | None] = {}

        async def child() -> None:
            bound = get_correlation()
            seen["request_id"] = bound.request_id if bound else None
            seen["org_id"] = bound.org_id if bound else None

        async def main() -> None:
            token = bind_correlation(ctx)
            try:
                await asyncio.create_task(child())
            finally:
                reset_correlation(token)

        asyncio.run(main())
        assert seen["request_id"] == "parent-req"
        assert seen["org_id"] == "org-1"

    def test_sibling_task_does_not_leak_across_reset(self):
        """Resetting in the parent before a sibling runs prevents leak."""
        ctx = CorrelationContext(request_id="first")
        seen: list[str | None] = []

        async def child() -> None:
            bound = get_correlation()
            seen.append(bound.request_id if bound else None)

        async def main() -> None:
            token = bind_correlation(ctx)
            await asyncio.create_task(child())
            reset_correlation(token)
            # After reset, a fresh sibling task sees no correlation.
            await asyncio.create_task(child())

        asyncio.run(main())
        assert seen == ["first", None]


# ===========================================================================
# Inbound X-Request-Id validation (§2 anti-log-injection)
# ===========================================================================


class TestInboundRequestIdValidation:
    def test_none_returns_none(self):
        assert validate_inbound_request_id(None) is None

    def test_empty_string_returns_none(self):
        assert validate_inbound_request_id("") is None
        assert validate_inbound_request_id("   ") is None

    def test_valid_hex_uuid_passes(self):
        raw = "550e8400e29b41d4a716446655440000"
        assert validate_inbound_request_id(raw) == raw

    def test_valid_dotted_slug_passes(self):
        raw = "deploy-2026-04-01.abc-123"
        assert validate_inbound_request_id(raw) == raw

    def test_whitespace_is_trimmed(self):
        assert validate_inbound_request_id("  abc-123  ") == "abc-123"

    def test_newline_rejected_to_prevent_log_injection(self):
        # A newline in the id could terminate the log line and inject a fake
        # second record — §2 forbids this.
        assert validate_inbound_request_id("abc\nFAKE LOG LINE") is None

    def test_control_characters_rejected(self):
        assert validate_inbound_request_id("abc\tdef") is None
        assert validate_inbound_request_id("abc\x00def") is None

    def test_json_structural_punctuation_rejected(self):
        # Quotes / braces would break JSON formatters downstream.
        for bad in ['"', "{", "}", ":", ",", " ", "/"]:
            assert validate_inbound_request_id(f"abc{bad}def") is None, f"unexpectedly accepted {bad!r}"

    @pytest.mark.parametrize("bad", ["abc<def", "abc>def", "abc\\def", "abc'def"])
    def test_html_and_shell_metacharacters_rejected(self, bad: str):
        assert validate_inbound_request_id(bad) is None

    def test_overlong_id_rejected(self):
        # 129 chars of allowed alphabet — one over the limit.
        raw = "a" * 129
        assert len(raw) == 129
        assert validate_inbound_request_id(raw) is None

    def test_exactly_128_chars_accepted(self):
        raw = "a" * 128
        assert validate_inbound_request_id(raw) == raw


class TestNewRequestId:
    def test_returns_32_char_hex_string(self):
        rid = new_request_id()
        assert len(rid) == 32
        int(rid, 16)  # parses as hex

    def test_returns_unique_values(self):
        ids = {new_request_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_generated_id_passes_own_validation(self):
        rid = new_request_id()
        assert validate_inbound_request_id(rid) == rid
