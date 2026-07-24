"""Class B runtime-security audit tests (PR-044).

Covers ADR-0005 §7.2 Class B events that have real code paths today:

* ``auth.login`` — ``routers/auth.py::login_local`` success + failure paths;
* ``policy.tool.denied`` (RBAC dimension) — ``rbac.py::require_rbac`` deny;
* ``policy.tool.denied`` (guardrail dimension) — ``guardrails/middleware.py``
  tool-call deny.

These are best-effort (post-action enqueue), so tests assert the outbox row
lands with the right action + outcome + reason_code, not any same-transaction
guarantee (that is the Class A concern, covered by ``test_audit_class_a.py``).

Fixture conventions mirror ``test_audit_outbox.py`` / ``test_iam_router_business.py``:
isolated SQLite via ``init_engine`` (full bootstrap). The app-layer login +
rbac paths use ``get_audit_sink()`` (reads the same engine); the harness-layer
guardrail path uses the registered ``OutboxAuditSink`` via ``set_tenant_event_sink``.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.contracts.events import AuditEvent
from deerflow.persistence.audit.model import AuditOutboxRow


async def _outbox_events(sf, *, action: str) -> list[AuditEvent]:
    async with sf() as session:
        rows = (await session.execute(select(AuditOutboxRow))).scalars().all()
    return [AuditEvent.model_validate_json(r.payload_json) for r in rows if AuditEvent.model_validate_json(r.payload_json).action == action]


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'classb.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    # Reset the audit-sink singleton so each test rebuilds it against THIS
    # test's factory (the lazy singleton otherwise pins the first test's sf).
    from app.gateway.audit_sink import reset_audit_sink_for_testing

    reset_audit_sink_for_testing()
    try:
        yield get_session_factory()
    finally:
        reset_audit_sink_for_testing()
        await close_engine()


# ===========================================================================
# auth.login — success + failure
# ===========================================================================


class TestAuthLoginAudit:
    @pytest.mark.anyio
    async def test_successful_login_emits_auth_login_success(self, sf):
        """A valid login enqueues ``auth.login`` with outcome=success."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.gateway.auth.models import User
        from app.gateway.auth.password import hash_password_async
        from app.gateway.routers import auth as auth_router

        user = User(id=uuid.uuid4(), email="audited@example.com", password_hash=await hash_password_async("Sup3rSecret!"))

        mock_provider = MagicMock()
        mock_provider.authenticate = AsyncMock(return_value=user)

        app = FastAPI()
        app.include_router(auth_router.router)
        app.state.session_factory = sf
        with patch("app.gateway.routers.auth.get_local_provider", return_value=mock_provider):
            with TestClient(app) as client:
                resp = client.post(
                    "/api/v1/auth/login/local",
                    data={"username": "audited@example.com", "password": "Sup3rSecret!"},
                )
                assert resp.status_code == 200, resp.text
        login_events = await _outbox_events(sf, action="auth.login")
        assert any(e.outcome == "success" for e in login_events)
        assert any(str(user.id) == e.actor.id for e in login_events)

    @pytest.mark.anyio
    async def test_failed_login_emits_auth_login_failure(self, sf):
        """A bad-password login enqueues ``auth.login`` with outcome=failure and
        reason_code=INVALID_CREDENTIALS, BEFORE the 401 is returned."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.gateway.routers import auth as auth_router

        mock_provider = MagicMock()
        mock_provider.authenticate = AsyncMock(return_value=None)  # bad credentials

        app = FastAPI()
        app.include_router(auth_router.router)
        app.state.session_factory = sf
        with patch("app.gateway.routers.auth.get_local_provider", return_value=mock_provider):
            with TestClient(app) as client:
                resp = client.post(
                    "/api/v1/auth/login/local",
                    data={"username": "real@example.com", "password": "WRONG-PASSWORD"},
                )
                assert resp.status_code == 401
        login_events = await _outbox_events(sf, action="auth.login")
        assert any(e.outcome == "failure" and e.reason_code == "INVALID_CREDENTIALS" for e in login_events)
        # The actor is the submitted email (no user row on failure).
        assert any(e.actor.id == "real@example.com" for e in login_events)


# ===========================================================================
# policy.tool.denied — RBAC dimension
# ===========================================================================


class TestRbacDenyAudit:
    @pytest.mark.anyio
    async def test_rbac_deny_emits_policy_tool_denied(self, sf):
        """A denied RBAC authorization enqueues ``policy.tool.denied`` with
        outcome=denied and the AuthorizeError code as reason_code."""
        from _router_auth_helpers import RBAC_DEFAULT_ORG_ID, bootstrap_rbac, make_rbac_test_app
        from fastapi.testclient import TestClient
        from test_rbac_runtime_routers import _probe_router_for

        from deerflow.contracts import Permission

        # Seed a user with the viewer role (no ADMIN_IAM_MANAGE) so the
        # decorated endpoint denies. bootstrap_rbac seeds org + roles + user
        # + membership + viewer binding.
        await bootstrap_rbac(sf, org_id=RBAC_DEFAULT_ORG_ID, role_name="org:viewer")

        # Reuse the proven probe router from test_rbac_runtime_routers — its
        # handler signature (path param + request: Request) is the shape
        # FastAPI resolves correctly under @require_rbac.
        app = make_rbac_test_app(sf=sf, org_id=RBAC_DEFAULT_ORG_ID)
        app.include_router(_probe_router_for(Permission.ADMIN_IAM_MANAGE))
        try:
            with TestClient(app) as client:
                resp = client.get("/probe/ADMIN_IAM_MANAGE/thread-1")
                assert resp.status_code == 403, resp.text
            denied = await _outbox_events(sf, action="policy.tool.denied")
            assert any(e.outcome == "denied" for e in denied)
            # reason_code is the AuthorizeError code (PERMISSION_DENIED etc.).
            assert all(e.reason_code for e in denied)
        finally:
            from app.gateway.authorize import reset_authorize_service_for_testing

            reset_authorize_service_for_testing()


# ===========================================================================
# policy.tool.denied — guardrail tool-call dimension (harness layer)
# ===========================================================================


class TestGuardrailDenyAudit:
    @pytest.mark.anyio
    async def test_guardrail_deny_emits_policy_tool_denied(self, sf):
        """The guardrail tool-call deny path emits ``policy.tool.denied`` via
        the ``emit_tenant_event`` shim (harness layer, never imports app). The
        shim routes through the registered ``OutboxAuditSink``."""
        from app.gateway.audit_sink import OutboxAuditSink
        from deerflow.guardrails.middleware import GuardrailMiddleware
        from deerflow.guardrails.provider import GuardrailDecision, GuardrailReason
        from deerflow.tenancy.audit_events import set_tenant_event_sink

        class _DenyAllProvider:
            name = "deny-all"

            def evaluate(self, request):
                return GuardrailDecision(
                    allow=False,
                    reasons=[GuardrailReason(code="oap.denied", message="all tools blocked")],
                    policy_id="test.deny.v1",
                )

            async def aevaluate(self, request):
                return self.evaluate(request)

        sink = OutboxAuditSink(sf)
        set_tenant_event_sink(sink)
        try:
            mw = GuardrailMiddleware(_DenyAllProvider())
            req = MagicMock()
            req.tool_call = {"name": "dangerous_tool", "args": {}, "id": "c1"}

            def handler(_request):
                raise AssertionError("handler must not be called on a deny")

            mw.wrap_tool_call(req, handler)
            # The shim schedules the async emit as fire-and-forget; poll until
            # the outbox row lands.
            denied = []
            for _ in range(50):
                await asyncio.sleep(0.02)
                denied = await _outbox_events(sf, action="policy.tool.denied")
                if denied:
                    break
            assert denied, "guardrail deny outbox row never landed"
            row = denied[0]
            assert row.outcome == "denied"
            assert row.payload["tool_name"] == "dangerous_tool"
            assert row.payload["policy_id"] == "test.deny.v1"
            assert row.payload["reason_code"] == "oap.denied"
        finally:
            set_tenant_event_sink(None)
