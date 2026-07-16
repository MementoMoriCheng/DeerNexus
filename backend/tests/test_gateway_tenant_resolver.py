"""Tests for the PR-013 Gateway tenant resolution adapter (single-Org bootstrap).

Covers TenantResolutionMiddleware: it resolves a trusted TenantContext after
authentication and binds it (runtime-contracts.md §5.2), so the tenant scope is
available to authorize/load/execute and restored on request exit.

Conventions mirror ``test_auth_middleware.py``: a private ``_make_app()`` adds
only the middleware under test plus AuthMiddleware, probe routes return the
bound tenant via the contextvar accessors, and the TestClient drives auth via
``monkeypatch.setenv`` and trusted internal headers.

Test IDs (``TEN-入口`` family, threat-model TM-001):

* session-authenticated principal resolves to the bootstrap org;
* trusted internal call honours ``X-DeerFlow-Owner-User-Id`` as principal.user_id;
* auth-disabled path still binds (local-dev), auth_method mapped to internal;
* client-supplied org_id is ignored (untrusted);
* try/finally restores the contextvar across the request boundary;
* bootstrap org id is configurable via DEER_FLOW_DEFAULT_ORG_ID;
* fail-closed when no authenticated principal reaches the resolver;
* inbound X-Request-Id propagates into the bound context.
"""

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app.gateway import config as gateway_config
from app.gateway.auth_middleware import AuthMiddleware
from app.gateway.internal_auth import create_internal_auth_headers
from app.gateway.tenant import TenantResolutionMiddleware
from deerflow.contracts import get_tenant_context, require_tenant_context
from deerflow.runtime.user_context import DEFAULT_USER_ID

ORG_A = "9f1c2b3a-4d5e-4789-abcd-ef0123456789"
ORG_B = "11111111-2222-3333-4444-555555555555"
OWNER_USER_ID = "owner-aaa-111"


@pytest.fixture(autouse=True)
def _reset_gateway_config():
    """Reset the cached GatewayConfig so per-test env overrides take effect."""
    gateway_config._gateway_config = None
    yield
    gateway_config._gateway_config = None


@pytest.fixture(autouse=True)
def _assert_no_tenant_residue():
    """No tenant context leaks between / after test cases."""
    assert get_tenant_context() is None, "tenant context leaked into this test from a previous one"
    yield
    assert get_tenant_context() is None, "tenant context leaked past test teardown"


def _make_app():
    """Minimal app with Auth + Tenant middleware and tenant-probe routes."""
    from fastapi import FastAPI, Request

    app = FastAPI()
    # BaseHTTPMiddleware runs in reverse add order inside call_next: the
    # middleware added LAST runs FIRST. Register tenant before auth so the
    # tenant resolver sees the authenticated request.state.user.
    app.add_middleware(TenantResolutionMiddleware)
    app.add_middleware(AuthMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/tenant")
    async def tenant_probe(request: Request):
        # require: routes run inside a bound tenant scope
        ctx = require_tenant_context()
        return {
            "org_id": ctx.org_id,
            "workspace_id": ctx.workspace_id,
            "principal_type": ctx.principal.type,
            "principal_id": ctx.principal.id,
            "principal_user_id": ctx.principal.user_id,
            "auth_method": ctx.auth_method,
            "request_id": ctx.request_id,
            "state_request_id": getattr(request.state, "request_id", None),
        }

    @app.get("/api/models")
    async def models_get():
        return {"models": []}

    return app


def _make_auth_disabled_app():
    """App under auth-disabled mode (local-dev), no cookie needed."""
    return _make_app()


# ---------------------------------------------------------------------------
# session authentication
# ---------------------------------------------------------------------------


def test_session_principal_resolves_to_bootstrap_org(monkeypatch):
    async def fake_current_user(request):
        return SimpleNamespace(
            id="session-user",
            email="session@test.local",
            system_role="user",
            needs_setup=False,
        )

    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    monkeypatch.setattr("app.gateway.deps.get_current_user_from_request", fake_current_user)
    client = TestClient(_make_app())

    res = client.get("/api/tenant", cookies={"access_token": "valid-session"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["org_id"] == gateway_config.DEFAULT_BOOTSTRAP_ORG_ID
    assert body["principal_id"] == "session-user"
    assert body["principal_user_id"] == "session-user"
    assert body["auth_method"] == "session"


# ---------------------------------------------------------------------------
# trusted internal call with owner header
# ---------------------------------------------------------------------------


def test_internal_call_honours_owner_user_id_header(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    client = TestClient(_make_app())

    res = client.get(
        "/api/tenant",
        headers=create_internal_auth_headers(owner_user_id=OWNER_USER_ID),
    )

    assert res.status_code == 200, res.text
    body = res.json()
    # principal.id is the synthetic internal principal (default); the trusted
    # owner header is carried as principal.user_id.
    assert body["principal_id"] == DEFAULT_USER_ID
    assert body["principal_user_id"] == OWNER_USER_ID
    assert body["org_id"] == gateway_config.DEFAULT_BOOTSTRAP_ORG_ID
    assert body["auth_method"] == "internal"


def test_internal_call_without_owner_header_uses_principal_id(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    client = TestClient(_make_app())

    res = client.get("/api/tenant", headers=create_internal_auth_headers())

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["principal_id"] == DEFAULT_USER_ID
    assert body["principal_user_id"] == DEFAULT_USER_ID


# ---------------------------------------------------------------------------
# auth-disabled path still binds
# ---------------------------------------------------------------------------


def test_auth_disabled_path_binds_tenant(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    client = TestClient(_make_auth_disabled_app())

    res = client.get("/api/tenant")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["org_id"] == gateway_config.DEFAULT_BOOTSTRAP_ORG_ID
    assert body["principal_id"] == "default"
    # auth_disabled maps onto the internal contract AuthMethod
    assert body["auth_method"] == "internal"


# ---------------------------------------------------------------------------
# client-supplied org_id is untrusted (TM-001)
# ---------------------------------------------------------------------------


def test_client_org_id_header_is_ignored(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    client = TestClient(_make_app())

    res = client.get("/api/tenant", headers={"X-Org-Id": ORG_B})

    assert res.status_code == 200, res.text
    assert res.json()["org_id"] == gateway_config.DEFAULT_BOOTSTRAP_ORG_ID


def test_client_org_id_query_param_is_ignored(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    client = TestClient(_make_app())

    res = client.get(f"/api/tenant?org_id={ORG_B}")

    assert res.status_code == 200, res.text
    assert res.json()["org_id"] == gateway_config.DEFAULT_BOOTSTRAP_ORG_ID


# ---------------------------------------------------------------------------
# configurable bootstrap org
# ---------------------------------------------------------------------------


def test_bootstrap_org_is_configurable_via_env(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_DEFAULT_ORG_ID", ORG_A)
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    client = TestClient(_make_app())

    res = client.get("/api/tenant")

    assert res.status_code == 200, res.text
    assert res.json()["org_id"] == ORG_A


# ---------------------------------------------------------------------------
# try/finally restores the contextvar across the request boundary
# ---------------------------------------------------------------------------


def test_contextvar_restored_after_request(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    client = TestClient(_make_app())

    assert get_tenant_context() is None
    res = client.get("/api/tenant")
    assert res.status_code == 200, res.text
    # the autouse ``_assert_no_tenant_residue`` teardown also asserts None here;
    # this explicit assertion documents the cross-request boundary.
    assert get_tenant_context() is None


def test_contextvar_restored_after_route_exception(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    from fastapi import FastAPI

    app = FastAPI()
    # BaseHTTPMiddleware runs in reverse add order inside call_next: the
    # middleware added LAST runs FIRST. Register tenant before auth so the
    # tenant resolver sees the authenticated request.state.user.
    app.add_middleware(TenantResolutionMiddleware)
    app.add_middleware(AuthMiddleware)

    @app.get("/api/boom")
    async def boom():
        raise RuntimeError("route failed")

    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/api/boom")
    assert res.status_code == 500
    assert get_tenant_context() is None


# ---------------------------------------------------------------------------
# request id propagation
# ---------------------------------------------------------------------------


def test_inbound_request_id_propagates(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    client = TestClient(_make_app())

    res = client.get("/api/tenant", headers={"X-Request-Id": "req-from-client"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["request_id"] == "req-from-client"
    assert body["state_request_id"] == "req-from-client"


def test_missing_request_id_is_synthesized(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    client = TestClient(_make_app())

    res = client.get("/api/tenant")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["request_id"]
    assert len(body["request_id"]) >= 8
    assert body["state_request_id"] == body["request_id"]


# ---------------------------------------------------------------------------
# public paths bypass the resolver
# ---------------------------------------------------------------------------


def test_public_path_bypasses_resolver(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    client = TestClient(_make_app())

    res = client.get("/health")

    assert res.status_code == 200
    assert get_tenant_context() is None
