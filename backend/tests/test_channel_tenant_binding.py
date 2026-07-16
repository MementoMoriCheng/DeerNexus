"""Tests for PR-014C: Channel / IM dispatch tenant scope.

Covers runtime-contracts.md §5.2 rule 3 (explicit, not implicit inheritance)
applied to the channel dispatch path, and rule 6 (no default-Org fallback):

* ``resolve_channel_tenant_context`` — Request-less tenant resolver mirroring
  the HTTP ``resolve_tenant_context`` for the channel path that has no
  ``Request`` (the dispatch task drives runs via HTTP loopback using the
  internal token + ``X-Deer-Flow-Owner-User-Id``);
* ``channel_tenant_scope`` — contextmanager that binds the tenant for the
  dispatch and restores on exit, no-op when no trusted owner is present;
* ``ChannelManager._handle_message`` — the dispatch task itself is now an
  auditable tenant-scoped entry point (the load-bearing integration case).

These tests follow the PR-012 / PR-013 / PR-014A sibling conventions: an
autouse ``_assert_no_tenant_residue`` fixture and ``_tenant`` / owner
builders. Async cases use per-test ``@pytest.mark.asyncio``.

Test IDs (``TEN-入口`` Channel family, threat-model TM-001 / TM-024).
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, MessageBus
from app.channels.store import ChannelStore
from app.gateway.tenant import (
    channel_tenant_scope,
    resolve_channel_tenant_context,
)
from deerflow.contracts import get_tenant_context

REQ_ID = "7b8e9f0a-1234-5678-9abc-def012345678"
OWNER = "owner-1"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _assert_no_tenant_residue():
    """No tenant context leaks between / after test cases (TEN-006)."""
    assert get_tenant_context() is None, "tenant context leaked into this test from a previous one"
    yield
    assert get_tenant_context() is None, "tenant context leaked past test teardown"


# ===========================================================================
# resolve_channel_tenant_context — Request-less resolver
# ===========================================================================


class TestChannelTenantResolution:
    """Verify the Request-less resolver mirrors the HTTP bootstrap resolver."""

    def test_org_id_came_from_config_not_synthesized(self):
        # The org must be the configured bootstrap org, never a hard-coded
        # literal or a client-supplied value (ADR-0002 §2.1; TM-001).
        from app.gateway.config import get_gateway_config

        tenant = resolve_channel_tenant_context(OWNER, REQ_ID)
        assert tenant.org_id == get_gateway_config().default_org_id

    def test_principal_named_from_trusted_owner(self):
        # No user object exists on the channel path; the trusted connection
        # owner is the principal, used for both id and user_id.
        tenant = resolve_channel_tenant_context(OWNER, REQ_ID)
        assert tenant.principal.type == "user"
        assert tenant.principal.id == OWNER
        assert tenant.principal.user_id == OWNER
        assert tenant.principal.display_name is None

    def test_auth_method_is_internal(self):
        # Channel runs always re-enter via the internal token, so the audit
        # surface records ``internal`` (matches the receiving-side mapping).
        tenant = resolve_channel_tenant_context(OWNER, REQ_ID)
        assert tenant.auth_method == "internal"

    def test_request_id_echoed(self):
        tenant = resolve_channel_tenant_context(OWNER, REQ_ID)
        assert tenant.request_id == REQ_ID

    def test_issued_at_is_timezone_aware(self):
        tenant = resolve_channel_tenant_context(OWNER, REQ_ID)
        assert tenant.issued_at.tzinfo is not None


# ===========================================================================
# channel_tenant_scope — contextmanager lifecycle
# ===========================================================================


class TestChannelTenantScope:
    """Verify bind/reset lifecycle and the no-op path for missing owner."""

    def test_scope_binds_then_restores(self):
        from app.gateway.config import get_gateway_config

        captured = {}
        with channel_tenant_scope(OWNER, REQ_ID):
            current = get_tenant_context()
            assert current is not None
            captured["org"] = current.org_id
        assert captured["org"] == get_gateway_config().default_org_id
        # Restored on normal exit (TEN-006).
        assert get_tenant_context() is None

    def test_scope_restores_on_exception(self):
        # §5.2 rule 2: binding must restore via try/finally on both exits.
        with pytest.raises(RuntimeError, match="boom"):
            with channel_tenant_scope(OWNER, REQ_ID):
                assert get_tenant_context() is not None
                raise RuntimeError("boom")
        assert get_tenant_context() is None

    def test_scope_is_noop_when_owner_missing(self):
        # Mirrors manager._owner_headers returning None for owner-less
        # dispatches: nothing is bound, leaving the loopback as the gate
        # (§5.2 rule 6 — no default-Org synthesis).
        with channel_tenant_scope(None, REQ_ID):
            assert get_tenant_context() is None
        assert get_tenant_context() is None


# ===========================================================================
# ChannelManager dispatch integration — load-bearing §5.2 rule 3 case
# ===========================================================================


def _run(coro):
    """Run an async coroutine synchronously (mirrors test_channels.py)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_mock_langgraph_client(capture_tenant):
    """Mock langgraph client that captures the tenant contextvar at run time."""
    mock_client = MagicMock()
    mock_client.threads.create = AsyncMock(return_value={"thread_id": "test-thread-123"})
    mock_client.threads.update = AsyncMock(return_value={"thread_id": "test-thread-123"})
    mock_client.threads.get = AsyncMock(return_value={"thread_id": "test-thread-123"})

    async def _runs_wait(*_args, **_kwargs):
        # Captured inside the dispatch task — proves the channel task itself
        # is tenant-scoped, not just the re-entered HTTP request.
        capture_tenant["value"] = get_tenant_context()
        return {"messages": [{"type": "human", "content": "hi"}, {"type": "ai", "content": "ok"}]}

    mock_client.runs.wait = AsyncMock(side_effect=_runs_wait)
    mock_client.runs.stream = AsyncMock()
    return mock_client


class TestManagerDispatchBindsTenant:
    """The dispatch task is itself a tenant-scoped entry point (PR-014C)."""

    def test_dispatch_binds_tenant_for_owner_scoped_message(self):
        async def go():
            from app.gateway.config import get_gateway_config

            bus = MessageBus()
            store = ChannelStore(path=Path(tempfile.mkdtemp()) / "store.json")
            manager = ChannelManager(bus=bus, store=store)

            captured: dict = {}

            outbound_received: list = []

            async def capture_outbound(msg):
                outbound_received.append(msg)

            bus.subscribe_outbound(capture_outbound)
            manager._client = _make_mock_langgraph_client(captured)
            await manager.start()

            inbound = InboundMessage(
                channel_name="test",
                chat_id="chat1",
                user_id="platform-user",
                owner_user_id=OWNER,
                connection_id="connection-1",
                text="hi",
            )
            await bus.publish_inbound(inbound)

            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if outbound_received:
                    break
                await asyncio.sleep(0.05)
            await manager.stop()

            assert "value" in captured, "runs.wait was not invoked by the dispatch"
            tenant = captured["value"]
            assert tenant is not None, "dispatch task had no tenant scope bound"
            assert tenant.org_id == get_gateway_config().default_org_id
            assert tenant.principal.user_id == OWNER

        _run(go())


class TestManagerDispatchNoOpWhenOwnerMissing:
    """Owner-less dispatch must still complete (loopback is the fail-closed gate)."""

    def test_dispatch_runs_without_binding_when_no_owner(self):
        async def go():
            bus = MessageBus()
            store = ChannelStore(path=Path(tempfile.mkdtemp()) / "store.json")
            manager = ChannelManager(bus=bus, store=store)

            captured: dict = {}

            outbound_received: list = []

            async def capture_outbound(msg):
                outbound_received.append(msg)

            bus.subscribe_outbound(capture_outbound)
            manager._client = _make_mock_langgraph_client(captured)
            await manager.start()

            inbound = InboundMessage(
                channel_name="test",
                chat_id="chat1",
                user_id="platform-user",
                text="hi",
            )
            await bus.publish_inbound(inbound)

            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if outbound_received:
                    break
                await asyncio.sleep(0.05)
            await manager.stop()

            assert "value" in captured, "runs.wait was not invoked by the dispatch"
            # No owner → no local scope; the loopback remains the gate.
            assert captured["value"] is None

        _run(go())
