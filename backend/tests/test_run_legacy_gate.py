"""Legacy Run resume gate (PR-056, ART-1750).

Tests ADR-0004 §12: a ``legacy_unpinned`` run may be read / cancelled /
archived but must NOT be resumed / continued once
``production.agent_release.enforce`` is on. The gate returns ``409
release_unpinned`` (non-retryable). When enforcement is off the gate is inert.

These tests exercise ``_gate_legacy_resume`` directly with a constructed
``RunRecord`` + ``Request`` so the exact envelope ``code`` is asserted
(testing-strategy §13.1: "assert the exact code, not a generic failure"),
without standing up the full run-manager / stream-bridge stack.

The cancel-then-stream POST path (``action`` present) bypasses the gate —
ADR §12 explicitly allows cancelling a legacy run. That branch is covered by
the existing ``stream_existing_run`` wiring (action=None gate) plus the
``test_cancel_run_idempotent`` suite.
"""

from __future__ import annotations

import types

import pytest
from fastapi import FastAPI, Request

from app.gateway.routers.thread_runs import _gate_legacy_resume
from deerflow.runtime import RunRecord, RunStatus

pytestmark = pytest.mark.anyio


def _stub_request() -> Request:
    """A Request whose ``app`` carries a minimal state (no real ASGI transport)."""
    app = FastAPI()
    scope = {
        "type": "http",
        "app": app,
        "headers": [],
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "state": {},
    }
    request = Request(scope)
    # The gate reads get_app_config() (module global), not request.state, so a
    # bare request is enough; request_id falls back to "unknown".
    return request


def _record(*, legacy_unpinned: bool) -> RunRecord:
    rec = RunRecord(
        run_id="run-legacy",
        thread_id="thread-1",
        assistant_id="lead_agent",
        status=RunStatus.interrupted,
        on_disconnect="cancel",
    )
    rec.legacy_unpinned = legacy_unpinned
    return rec


class TestLegacyResumeGate:
    async def test_legacy_unpinned_blocked_when_enforced(self, monkeypatch):
        """ART-1751: legacy + enforce → 409 release_unpinned (exact code)."""
        from app.gateway.routers import thread_runs as mod
        from deerflow.config.production_config import ProductionAgentReleaseConfig

        cfg = types.SimpleNamespace()
        cfg.production = types.SimpleNamespace(agent_release=ProductionAgentReleaseConfig(enforce=True, default_channel="prod"))
        monkeypatch.setattr(mod, "get_app_config", lambda: cfg)

        with pytest.raises(Exception) as exc_info:
            _gate_legacy_resume(_stub_request(), "run-legacy", _record(legacy_unpinned=True))

        # The gate raises an HTTPException whose detail is the ContractError dict.
        http_exc = exc_info.value
        detail = http_exc.detail  # type: ignore[attr-defined]
        assert http_exc.status_code == 409  # type: ignore[attr-defined]
        assert detail["code"] == "release_unpinned"
        assert detail["retryable"] is False
        assert "legacy-unpinned" in detail["message"].lower()

    async def test_pinned_run_not_blocked_when_enforced(self, monkeypatch):
        """ART-1752: pinned (legacy_unpinned=False) + enforce → no raise."""
        from app.gateway.routers import thread_runs as mod
        from deerflow.config.production_config import ProductionAgentReleaseConfig

        cfg = types.SimpleNamespace()
        cfg.production = types.SimpleNamespace(agent_release=ProductionAgentReleaseConfig(enforce=True, default_channel="prod"))
        monkeypatch.setattr(mod, "get_app_config", lambda: cfg)

        # Must not raise.
        _gate_legacy_resume(_stub_request(), "run-pinned", _record(legacy_unpinned=False))

    async def test_legacy_run_not_blocked_when_enforcement_off(self, monkeypatch):
        """ART-1753: enforce=false → gate inert (today's behaviour preserved)."""
        from app.gateway.routers import thread_runs as mod
        from deerflow.config.production_config import ProductionAgentReleaseConfig

        cfg = types.SimpleNamespace()
        cfg.production = types.SimpleNamespace(agent_release=ProductionAgentReleaseConfig(enforce=False, default_channel="dev"))
        monkeypatch.setattr(mod, "get_app_config", lambda: cfg)

        # A legacy run must resume freely when the gate is off.
        _gate_legacy_resume(_stub_request(), "run-legacy", _record(legacy_unpinned=True))

    async def test_release_unpinned_is_non_retryable_contract(self):
        """Lock the ErrorCode classification (test_contracts_base already pins this)."""
        from deerflow.contracts.errors import ErrorCode, is_retryable_code

        assert not is_retryable_code(ErrorCode.RELEASE_UNPINNED.value)
