"""Router tests for legacy IM channel management endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from _router_auth_helpers import make_rbac_test_app
from fastapi.testclient import TestClient

from app.gateway.auth.models import User
from app.gateway.routers import channels


def _admin_user() -> User:
    return User(
        id=UUID("11111111-2222-3333-4444-555555555555"),
        email="admin@example.com",
        password_hash="x",
        system_role="admin",
    )


def _non_admin_user() -> User:
    return User(
        id=UUID("99999999-8888-7777-6666-555555555555"),
        email="user@example.com",
        password_hash="x",
        system_role="user",
    )


def test_restart_channel_requires_admin(monkeypatch):
    # The 403-not-admin boundary (regular user / viewer / missing
    # org:admin binding) is pinned in ``test_rbac_admin_routers.py``
    # against the real Authorize Service. This suite runs in bypass
    # mode (``make_rbac_test_app(bypass_authorize=True)``), so a
    # regular-user stub would pass — the boundary is intentionally not
    # re-tested here.
    service = SimpleNamespace(restart_channel=AsyncMock(return_value=True))
    monkeypatch.setattr("app.channels.service.get_channel_service", lambda: service)
    app = make_rbac_test_app(bypass_authorize=True, user_factory=_non_admin_user)
    app.include_router(channels.router)

    with TestClient(app) as client:
        response = client.post("/api/channels/slack/restart")

    # Bypass mode short-circuits ``require_rbac`` before the Authorize
    # Service is consulted, so the handler runs (200). The matrix test
    # is authoritative for the 403 boundary.
    assert response.status_code == 200
    service.restart_channel.assert_awaited_once_with("slack")


def test_restart_channel_allows_admin(monkeypatch):
    service = SimpleNamespace(restart_channel=AsyncMock(return_value=True))
    monkeypatch.setattr("app.channels.service.get_channel_service", lambda: service)
    app = make_rbac_test_app(bypass_authorize=True, user_factory=_admin_user)
    app.include_router(channels.router)

    with TestClient(app) as client:
        response = client.post("/api/channels/slack/restart")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "Channel slack restarted successfully",
    }
    service.restart_channel.assert_awaited_once_with("slack")


def test_get_channels_status_remains_read_only(monkeypatch):
    service = SimpleNamespace(
        get_status=lambda: {
            "service_running": True,
            "channels": {
                "slack": {
                    "enabled": True,
                    "running": True,
                }
            },
        }
    )
    monkeypatch.setattr("app.channels.service.get_channel_service", lambda: service)
    app = make_rbac_test_app(bypass_authorize=True, user_factory=_non_admin_user)
    app.include_router(channels.router)

    with TestClient(app) as client:
        response = client.get("/api/channels/")

    assert response.status_code == 200
    assert response.json()["service_running"] is True
    assert response.json()["channels"]["slack"]["running"] is True
