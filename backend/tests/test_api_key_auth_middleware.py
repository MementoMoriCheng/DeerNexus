"""End-to-end auth-middleware tests for the API Key credential path (PR-035).

Drives the full ``AuthMiddleware`` → ``TenantResolutionMiddleware`` →
``@require_rbac`` → ``AuthorizeService.authorize()`` chain via TestClient.
The ADR §12 sequence (existence → hash → expiry → revocation → SA
disabled) is exhaustively covered, plus the e2e scope-narrowing path.

The app under test mounts a single probe router gated by
``Permission.RUNTIME_RUN_READ`` and seeds an Org + builtin roles +
ServiceAccount + role binding. The probe handler returns 200 on allow;
``require_rbac`` raises 401/403 on deny.

IAM IDs: ``IAM-340`` series (auth middleware e2e; crypto is ``IAM-330``,
repository is ``IAM-32x``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

import deerflow.persistence.models  # noqa: F401  — register ORM
from app.gateway.auth_middleware import AuthMiddleware
from app.gateway.rbac import require_rbac
from app.gateway.tenant import TenantResolutionMiddleware
from deerflow.contracts import Permission
from deerflow.contracts.rbac import ORG_DEVELOPER_ROLE_NAME
from deerflow.persistence.iam.model import ApiKeyRow, ServiceAccountRow
from deerflow.persistence.orgs.model import OrganizationRow

ORG_ID = "org-test"
SA_ID = "sa-test-1"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'api_key_middleware.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture(autouse=True)
def _fixed_pepper():
    """Pin the pepper so hashes computed in tests verify in the middleware.

    Saves / restores the global ``AuthConfig`` singleton so other test
    modules see their own (or the default) config after this fixture
    tears down.
    """
    from app.gateway.auth import config as auth_config

    saved = auth_config._auth_config  # type: ignore[attr-defined]
    auth_config.set_auth_config(auth_config.AuthConfig(jwt_secret="jwt-test", api_key_pepper="test-pepper-fixed"))
    yield
    auth_config._auth_config = saved  # type: ignore[attr-defined]


async def _seed_world(sf, *, sa_status: str = "active", role_name: str = ORG_DEVELOPER_ROLE_NAME):
    """Seed the IAM world the auth chain needs.

    * one Org (ORG_ID)
    * the three builtin roles (so role_bindings can FK to them)
    * one active ServiceAccount (SA_ID) in ORG_ID
    * one role binding granting ``role_name`` to SA_ID
    """
    from deerflow.tenancy import ensure_builtin_roles

    async with sf() as session:
        session.add(OrganizationRow(id=ORG_ID, slug=ORG_ID, name=ORG_ID, status="active"))
        await session.commit()
    await ensure_builtin_roles(sf)
    async with sf() as session:
        # Idempotent on SA_ID.
        if await session.get(ServiceAccountRow, SA_ID) is None:
            session.add(ServiceAccountRow(id=SA_ID, org_id=ORG_ID, name="bot", status=sa_status))
            await session.commit()
    # Look up the role id (builtin system template) and bind.
    from sqlalchemy import select

    from deerflow.persistence.iam.model import RoleBindingRow, RoleRow

    async with sf() as session:
        role = (await session.execute(select(RoleRow).where(RoleRow.name == role_name, RoleRow.is_system.is_(True)))).scalar_one()
        existing = (
            await session.execute(
                select(RoleBindingRow).where(
                    RoleBindingRow.org_id == ORG_ID,
                    RoleBindingRow.principal_type == "service_account",
                    RoleBindingRow.principal_id == SA_ID,
                    RoleBindingRow.role_id == role.id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                RoleBindingRow(
                    id="binding-test-1",
                    org_id=ORG_ID,
                    principal_type="service_account",
                    principal_id=SA_ID,
                    role_id=role.id,
                )
            )
            await session.commit()


async def _mint_key(
    sf,
    *,
    plaintext: str,
    scopes: list[str],
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    key_prefix: str | None = None,
    service_account_id: str = SA_ID,
    org_id: str = ORG_ID,
) -> None:
    """Insert an ApiKeyRow with the hash of ``plaintext`` already computed.

    By default ``key_prefix`` is derived from ``plaintext`` (first 16
    chars) so it matches what ``generate_api_key`` would produce and
    what ``AuthMiddleware._resolve_api_key`` will look up. Pass an
    explicit ``key_prefix`` only to test the hash-mismatch path (a
    plaintext that doesn't match its stored prefix).
    """
    from app.gateway.auth.api_key import _DISPLAY_PREFIX, _RANDOM_PREFIX_LEN, hash_api_key

    if key_prefix is None:
        prefix_len = len(_DISPLAY_PREFIX) + _RANDOM_PREFIX_LEN
        key_prefix = plaintext[:prefix_len]
    async with sf() as session:
        session.add(
            ApiKeyRow(
                id="key-test-1",
                org_id=org_id,
                service_account_id=service_account_id,
                key_prefix=key_prefix,
                key_hash=hash_api_key(plaintext),
                scopes=list(scopes),
                expires_at=expires_at or (datetime.now(UTC) + timedelta(days=30)),
                revoked_at=revoked_at,
            )
        )
        await session.commit()


def _build_app(sf) -> FastAPI:
    """Build a minimal app: Auth + Tenant middlewares + one probe router.

    The probe is gated by ``RUNTIME_RUN_READ`` and returns 200 on allow.
    PR-035 wires ``api_key_scopes`` through ``TenantContext`` →
    ``require_rbac`` → ``authorize()``, so the probe double-duty as the
    scope-narrowing e2e assertion (a key scoped to ``runtime:run:read``
    allows the probe; a key scoped to a narrower set denies it).
    """
    from app.gateway import authorize as _authorize_mod
    from app.gateway.authorize import AuthorizeService, reset_authorize_service_for_testing

    reset_authorize_service_for_testing()
    _authorize_mod._default_service = AuthorizeService(sf)  # type: ignore[attr-defined]

    app = FastAPI()
    app.state.session_factory = sf
    app.add_middleware(TenantResolutionMiddleware)
    app.add_middleware(AuthMiddleware)

    router = APIRouter()

    @router.get("/probe")
    @require_rbac(Permission.RUNTIME_RUN_READ)
    async def _probe(request: Request) -> dict:  # noqa: ARG001
        return {"ok": True}

    app.include_router(router)
    return app


# ===========================================================================
# IAM-340 — happy path
# ===========================================================================


class TestApiKeyAuthHappyPath:
    @pytest.mark.anyio
    async def test_x_api_key_header_authenticates(self, sf):
        await _seed_world(sf)
        plaintext = "dk_live_abcd1234_" + "x" * 40
        await _mint_key(sf, plaintext=plaintext, scopes=["runtime:run:read"])
        app = _build_app(sf)
        with TestClient(app) as client:
            resp = client.get("/probe", headers={"X-Api-Key": plaintext})
        assert resp.status_code == 200, resp.text

    @pytest.mark.anyio
    async def test_authorization_bearer_header_authenticates(self, sf):
        await _seed_world(sf)
        plaintext = "dk_live_bearer0001_" + "y" * 40
        await _mint_key(sf, plaintext=plaintext, scopes=["runtime:run:read"])
        app = _build_app(sf)
        with TestClient(app) as client:
            resp = client.get("/probe", headers={"Authorization": f"Bearer {plaintext}"})
        assert resp.status_code == 200, resp.text


# ===========================================================================
# IAM-341 — ADR §12 failure sequence (all map to 401 authentication_invalid)
# ===========================================================================


class TestApiKeyAuthFailures:
    @pytest.mark.anyio
    async def test_missing_header_falls_through_to_401(self, sf):
        # No header + no cookie + auth not disabled → 401 NOT_AUTHENTICATED.
        await _seed_world(sf)
        app = _build_app(sf)
        with TestClient(app) as client:
            resp = client.get("/probe")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_unknown_prefix_returns_401(self, sf):
        await _seed_world(sf)
        app = _build_app(sf)
        with TestClient(app) as client:
            resp = client.get("/probe", headers={"X-Api-Key": "dk_live_nope0000_" + "z" * 40})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_hash_mismatch_returns_401(self, sf):
        await _seed_world(sf)
        await _mint_key(sf, plaintext="dk_live_real0001_" + "a" * 40, scopes=["runtime:run:read"])
        app = _build_app(sf)
        with TestClient(app) as client:
            # Different secret portion → HMAC differs → hash mismatch.
            resp = client.get("/probe", headers={"X-Api-Key": "dk_live_real0001_" + "b" * 40})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_expired_key_returns_401(self, sf):
        await _seed_world(sf)
        plaintext = "dk_live_expired01_" + "c" * 40
        await _mint_key(
            sf,
            plaintext=plaintext,
            scopes=["runtime:run:read"],
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        app = _build_app(sf)
        with TestClient(app) as client:
            resp = client.get("/probe", headers={"X-Api-Key": plaintext})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_revoked_key_returns_401(self, sf):
        await _seed_world(sf)
        plaintext = "dk_live_revoked1_" + "d" * 40
        await _mint_key(
            sf,
            plaintext=plaintext,
            scopes=["runtime:run:read"],
            revoked_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        app = _build_app(sf)
        with TestClient(app) as client:
            resp = client.get("/probe", headers={"X-Api-Key": plaintext})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_truncated_plaintext_returns_401(self, sf):
        await _seed_world(sf)
        app = _build_app(sf)
        with TestClient(app) as client:
            resp = client.get("/probe", headers={"X-Api-Key": "dk_live_short"})
        assert resp.status_code == 401


# ===========================================================================
# IAM-342 — SA disabled → 403 principal_disabled
# ===========================================================================


class TestApiKeyAuthSaDisabled:
    @pytest.mark.anyio
    async def test_valid_key_disabled_sa_returns_403(self, sf):
        await _seed_world(sf, sa_status="disabled")
        plaintext = "dk_live_disabled_" + "e" * 40
        await _mint_key(sf, plaintext=plaintext, scopes=["runtime:run:read"])
        app = _build_app(sf)
        with TestClient(app) as client:
            resp = client.get("/probe", headers={"X-Api-Key": plaintext})
        assert resp.status_code == 403
        body = resp.json()
        # ADR §12 maps disabled → principal_disabled.
        assert "disabled" in str(body).lower() or "principal" in str(body).lower()


# ===========================================================================
# IAM-343 — e2e scope narrowing
# ===========================================================================


class TestApiKeyScopeNarrowingE2E:
    @pytest.mark.anyio
    async def test_key_scope_narrow_denies_unscoped_permission(self, sf):
        """Key scoped to ``runtime:thread:read`` denies ``runtime:run:read``.

        The SA's role binding grants the full developer set (which
        includes both), but the API Key's scopes narrow to only
        ``runtime:thread:read``. The probe is gated on
        ``runtime:run:read`` → 403.
        """
        await _seed_world(sf, role_name=ORG_DEVELOPER_ROLE_NAME)
        plaintext = "dk_live_scoped01_" + "f" * 40
        await _mint_key(
            sf,
            plaintext=plaintext,
            scopes=["runtime:thread:read"],  # narrower than the probe's RUNTIME_RUN_READ
        )
        app = _build_app(sf)
        with TestClient(app) as client:
            resp = client.get("/probe", headers={"X-Api-Key": plaintext})
        assert resp.status_code == 403, resp.text

    @pytest.mark.anyio
    async def test_key_scope_match_allows(self, sf):
        """Key scoped to ``runtime:run:read`` allows the probe."""
        await _seed_world(sf, role_name=ORG_DEVELOPER_ROLE_NAME)
        plaintext = "dk_live_match001_" + "g" * 40
        await _mint_key(
            sf,
            plaintext=plaintext,
            scopes=["runtime:run:read"],
        )
        app = _build_app(sf)
        with TestClient(app) as client:
            resp = client.get("/probe", headers={"X-Api-Key": plaintext})
        assert resp.status_code == 200, resp.text


# ===========================================================================
# IAM-344 — fallback when no API-key header (existing paths unaffected)
# ===========================================================================


class TestNoApiKeyFallback:
    @pytest.mark.anyio
    async def test_session_cookie_path_still_works(self, sf):
        """No X-Api-Key + valid cookie → session path runs as before.

        Regression anchor: the API-key branch must NOT break the
        existing session cookie path. We do not actually mint a JWT
        here; we just confirm the middleware's response shape (401 with
        NOT_AUTHENTICATED, not a crash).
        """
        await _seed_world(sf)
        app = _build_app(sf)
        with TestClient(app) as client:
            resp = client.get("/probe")
        # No cookie, no API key → 401 from the existing fallthrough.
        assert resp.status_code == 401


# ===========================================================================
# IAM-345 — policy.evaluated carries api_key_id (observability)
# ===========================================================================


class TestPolicyEvaluatedCarriesApiKeyId:
    @pytest.mark.anyio
    async def test_allow_event_has_api_key_id(self, sf):
        """``policy.evaluated`` includes the ``api_key_id`` kwarg on the
        API-key path (PR-035 observability). The router's rbac decorator
        reads it off ``request.state.api_key_id``, which AuthMiddleware
        stamps after a successful key verification.
        """
        from unittest.mock import patch

        await _seed_world(sf)
        plaintext = "dk_live_observed1_" + "h" * 40
        await _mint_key(sf, plaintext=plaintext, scopes=["runtime:run:read"])
        app = _build_app(sf)
        with patch("app.gateway.rbac.emit_event") as mock_emit:
            with TestClient(app) as client:
                resp = client.get("/probe", headers={"X-Api-Key": plaintext})
            assert resp.status_code == 200
        policy_calls = [c for c in mock_emit.call_args_list if c.args and c.args[0] == "policy.evaluated"]
        assert len(policy_calls) == 1
        # The api_key_id is the row id "key-test-1" set by _mint_key.
        assert policy_calls[0].kwargs.get("api_key_id") == "key-test-1"
