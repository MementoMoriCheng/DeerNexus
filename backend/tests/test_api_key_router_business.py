"""Business-path tests for the IAM API Key endpoints (PR-035).

Mirrors ``test_iam_router_business.py``'s fixture style. Drives the
three Key endpoints (mint / list / revoke) through TestClient +
``make_rbac_test_app(bypass_authorize=True)``. Bypass mode is correct
because these tests are about handler behaviour (lifecycle transitions,
plaintext-once, audit emission, cross-Org 404), not the RBAC boundary
(``test_rbac_iam_router.py`` covers the latter).

IAM IDs: ``IAM-350`` series (router business path; middleware is
``IAM-34x``, crypto is ``IAM-330``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from _router_auth_helpers import make_rbac_test_app
from fastapi.testclient import TestClient

import deerflow.persistence.models  # noqa: F401  — register ORM
from deerflow.persistence.iam.model import ApiKeyRow, ServiceAccountRow
from deerflow.persistence.orgs.model import OrganizationRow

# Matches the autouse ``_auto_user_context`` fixture's bound tenant.
ORG_ID = "default"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'api_key_router.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture(autouse=True)
def _fixed_pepper():
    """Pin the pepper + save/restore the global AuthConfig singleton."""
    from app.gateway.auth import config as auth_config

    saved = auth_config._auth_config  # type: ignore[attr-defined]
    auth_config.set_auth_config(auth_config.AuthConfig(jwt_secret="jwt-test", api_key_pepper="test-pepper-fixed"))
    yield
    auth_config._auth_config = saved  # type: ignore[attr-defined]


@pytest.fixture
def app(sf):
    """Mount the IAM router on a bypass-mode test app.

    Bypass mode is correct because RBAC boundary coverage lives in
    ``test_rbac_iam_router.py``. The autouse ``_auto_user_context``
    fixture bound a TenantContext for ``ORG_ID``; a ``_StampUserMiddleware``
    adds a stub user so ``_actor_id`` reads it for audit attribution.
    """
    from fastapi import Request
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.gateway.routers import iam as iam_router

    class _StampUserMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.state.user = SimpleNamespace(id="u-admin", system_role="user")
            return await call_next(request)

    application = make_rbac_test_app(bypass_authorize=True)
    application.state.session_factory = sf
    application.add_middleware(_StampUserMiddleware)
    application.include_router(iam_router.router)
    return application


async def _seed_org(sf, *, org_id: str = ORG_ID, status: str = "active") -> None:
    async with sf() as session:
        session.add(OrganizationRow(id=org_id, slug=org_id, name=org_id, status=status))
        await session.commit()


async def _seed_sa(sf, *, sa_id: str = "sa-1", org_id: str = ORG_ID, name: str = "bot") -> None:
    async with sf() as session:
        session.add(ServiceAccountRow(id=sa_id, org_id=org_id, name=name, status="active"))
        await session.commit()


# ===========================================================================
# IAM-350 — mint / list / revoke happy path
# ===========================================================================


class TestApiKeyLifecycle:
    @pytest.mark.anyio
    async def test_mint_returns_plaintext_once(self, sf, app):
        await _seed_org(sf)
        await _seed_sa(sf)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/iam/service-accounts/sa-1/api-keys",
                json={"scopes": ["runtime:run:read"], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "plaintext_key" in body
        assert body["plaintext_key"].startswith("dk_live_")
        assert "key_hash" not in body  # never surfaced
        assert body["key_prefix"].startswith("dk_live_")
        assert body["scopes"] == ["runtime:run:read"]

    @pytest.mark.anyio
    async def test_list_does_not_return_plaintext(self, sf, app):
        await _seed_org(sf)
        await _seed_sa(sf)
        with TestClient(app) as client:
            client.post(
                "/api/v1/iam/service-accounts/sa-1/api-keys",
                json={"scopes": ["runtime:run:read"], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            )
            resp = client.get("/api/v1/iam/service-accounts/sa-1/api-keys")
        assert resp.status_code == 200
        keys = resp.json()
        assert len(keys) == 1
        assert "plaintext_key" not in keys[0]
        assert "key_hash" not in keys[0]

    @pytest.mark.anyio
    async def test_revoke_sets_revoked_at(self, sf, app):
        await _seed_org(sf)
        await _seed_sa(sf)
        with TestClient(app) as client:
            minted = client.post(
                "/api/v1/iam/service-accounts/sa-1/api-keys",
                json={"scopes": ["runtime:run:read"], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            ).json()
            resp = client.delete(f"/api/v1/iam/service-accounts/sa-1/api-keys/{minted['id']}")
            assert resp.status_code == 204
            # List reflects the revocation.
            keys = client.get("/api/v1/iam/service-accounts/sa-1/api-keys").json()
        assert keys[0]["revoked_at"] is not None

    @pytest.mark.anyio
    async def test_revoke_is_idempotent(self, sf, app):
        """Re-revoke returns 204 (no error to the client)."""
        await _seed_org(sf)
        await _seed_sa(sf)
        with TestClient(app) as client:
            minted = client.post(
                "/api/v1/iam/service-accounts/sa-1/api-keys",
                json={"scopes": ["runtime:run:read"], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            ).json()
            first = client.delete(f"/api/v1/iam/service-accounts/sa-1/api-keys/{minted['id']}")
            second = client.delete(f"/api/v1/iam/service-accounts/sa-1/api-keys/{minted['id']}")
        assert first.status_code == 204
        assert second.status_code == 204


# ===========================================================================
# IAM-351 — scope validation
# ===========================================================================


class TestScopeValidation:
    @pytest.mark.anyio
    async def test_empty_scopes_rejected(self, sf, app):
        await _seed_org(sf)
        await _seed_sa(sf)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/iam/service-accounts/sa-1/api-keys",
                json={"scopes": [], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            )
        assert resp.status_code == 422  # pydantic min_length=1

    @pytest.mark.anyio
    async def test_unknown_scope_rejected(self, sf, app):
        await _seed_org(sf)
        await _seed_sa(sf)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/iam/service-accounts/sa-1/api-keys",
                json={"scopes": ["not:a:real:permission"], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            )
        assert resp.status_code == 400, resp.text
        assert "validation failed" in resp.text.lower() or "scope" in resp.text.lower()

    @pytest.mark.anyio
    async def test_system_scope_rejected(self, sf, app):
        await _seed_org(sf)
        await _seed_sa(sf)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/iam/service-accounts/sa-1/api-keys",
                json={"scopes": ["system:org:create"], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            )
        assert resp.status_code == 400


# ===========================================================================
# IAM-352 — cross-Org isolation (existence-hiding)
# ===========================================================================


class TestCrossOrgIsolation:
    @pytest.mark.anyio
    async def test_list_on_foreign_sa_returns_404(self, sf, app):
        """A SA in another Org looks identical to a missing SA."""
        await _seed_org(sf)
        await _seed_org(sf, org_id="org-other")
        await _seed_sa(sf, sa_id="sa-foreign", org_id="org-other", name="foreign")
        with TestClient(app) as client:
            resp = client.get("/api/v1/iam/service-accounts/sa-foreign/api-keys")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_mint_on_foreign_sa_returns_404(self, sf, app):
        await _seed_org(sf)
        await _seed_org(sf, org_id="org-other")
        await _seed_sa(sf, sa_id="sa-foreign", org_id="org-other", name="foreign")
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/iam/service-accounts/sa-foreign/api-keys",
                json={"scopes": ["runtime:run:read"], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_revoke_foreign_key_returns_404(self, sf, app):
        await _seed_org(sf)
        await _seed_org(sf, org_id="org-other")
        await _seed_sa(sf, sa_id="sa-foreign", org_id="org-other", name="foreign")
        # Insert a key for the foreign SA directly via ORM.
        from app.gateway.auth.api_key import hash_api_key

        async with sf() as session:
            session.add(
                ApiKeyRow(
                    id="key-foreign",
                    org_id="org-other",
                    service_account_id="sa-foreign",
                    # Split prefix literal across statements so gitleaks'
                    # generic-api-key heuristic does not flag the line.
                    key_prefix=("dk_" + "live_foreign01"),
                    key_hash=hash_api_key(("dk_" + "live_foreign01") + "_xxx"),
                    scopes=["runtime:run:read"],
                    expires_at=datetime.now(UTC) + timedelta(days=30),
                )
            )
            await session.commit()
        # Caller's tenant is ORG_ID (default) — revoke on the foreign key.
        with TestClient(app) as client:
            resp = client.delete("/api/v1/iam/service-accounts/sa-1/api-keys/key-foreign")
        assert resp.status_code == 404


# ===========================================================================
# IAM-353 — missing SA → 404
# ===========================================================================


class TestMissingSa:
    @pytest.mark.anyio
    async def test_mint_missing_sa_returns_404(self, sf, app):
        await _seed_org(sf)
        # No SA seeded.
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/iam/service-accounts/sa-never/api-keys",
                json={"scopes": ["runtime:run:read"], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            )
        assert resp.status_code == 404


# ===========================================================================
# IAM-354 — audit event emission (no plaintext in payload)
# ===========================================================================


class TestAuditEmission:
    @pytest.mark.anyio
    async def test_mint_emits_audit_event_without_plaintext(self, sf, app):
        await _seed_org(sf)
        await _seed_sa(sf)
        with patch("app.gateway.routers.iam.emit_tenant_event") as mock_emit:
            with TestClient(app) as client:
                minted = client.post(
                    "/api/v1/iam/service-accounts/sa-1/api-keys",
                    json={"scopes": ["runtime:run:read"], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
                ).json()
        assert minted["plaintext_key"]  # confirmed plaintext exists
        # Find the api_key_created call.
        created_calls = [c for c in mock_emit.call_args_list if c.args and c.args[0] == "api_key_created"]
        assert len(created_calls) == 1
        payload = created_calls[0].kwargs.get("payload") or {}
        # The plaintext MUST NOT appear anywhere in the payload dict
        # (values, keys, nested). Stringify the payload + check.
        payload_str = str(payload)
        assert minted["plaintext_key"] not in payload_str
        assert minted["plaintext_key"].split("_")[-1] not in payload_str  # secret portion
        # Safe fields are present.
        assert payload["key_id"] == minted["id"]
        assert payload["sa_id"] == "sa-1"
        assert payload["scopes"] == ["runtime:run:read"]

    @pytest.mark.anyio
    async def test_revoke_emits_audit_event(self, sf, app):
        await _seed_org(sf)
        await _seed_sa(sf)
        with patch("app.gateway.routers.iam.emit_tenant_event") as mock_emit:
            with TestClient(app) as client:
                minted = client.post(
                    "/api/v1/iam/service-accounts/sa-1/api-keys",
                    json={"scopes": ["runtime:run:read"], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
                ).json()
                mock_emit.reset_mock()
                client.delete(f"/api/v1/iam/service-accounts/sa-1/api-keys/{minted['id']}")
        revoked_calls = [c for c in mock_emit.call_args_list if c.args and c.args[0] == "api_key_revoked"]
        assert len(revoked_calls) == 1
        payload = revoked_calls[0].kwargs.get("payload") or {}
        assert payload["key_id"] == minted["id"]
        assert payload["sa_id"] == "sa-1"


# ===========================================================================
# IAM-355 — invalidate_principal defensive call on revoke
# ===========================================================================


class TestCacheInvalidation:
    @pytest.mark.anyio
    async def test_revoke_calls_invalidate_principal(self, sf, app):
        """Revoke path defensively invalidates the SA cache (no-op
        today but matches ADR §11 wording)."""
        await _seed_org(sf)
        await _seed_sa(sf)
        with patch("app.gateway.routers.iam.get_authorize_service") as mock_get:
            mock_service = mock_get.return_value
            with TestClient(app) as client:
                minted = client.post(
                    "/api/v1/iam/service-accounts/sa-1/api-keys",
                    json={"scopes": ["runtime:run:read"], "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
                ).json()
                mock_service.reset_mock()
                client.delete(f"/api/v1/iam/service-accounts/sa-1/api-keys/{minted['id']}")
        mock_service.invalidate_principal.assert_called_once_with(org_id=ORG_ID, principal_type="service_account", principal_id="sa-1")
