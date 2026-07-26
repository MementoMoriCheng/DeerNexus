"""Business + RBAC tests for the release-channel router (PR-053).

Drives ``app.gateway.routers.agent_artifacts`` channel endpoints through
TestClient: ``:promote`` / ``:rollback`` CAS + ``GET`` channel/event reads.
The business path uses ``make_rbac_test_app(bypass_authorize=True)``; the
RBAC matrix uses ``make_rbac_test_app(sf=sf)`` so the real ``authorize()``
decides (developer only carries ``studio:release:promote_dev``).

Mirrors ``test_agent_artifact_router.py`` conventions: ``ORG_ID = "default"``
matches the autouse ``_auto_user_context`` fixture; ``_seed_org`` + helper
callers wrap the HTTP flow.

Channel IDs: ``ART-810`` (router business) / ``ART-910`` (router RBAC).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from _router_auth_helpers import bootstrap_rbac, make_rbac_test_app
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import deerflow.persistence.models  # noqa: F401  — register ORM
from deerflow.contracts.rbac import (
    BUILTIN_ROLE_PERMISSIONS,
    ORG_ADMIN_ROLE_NAME,
    ORG_DEVELOPER_ROLE_NAME,
    ORG_VIEWER_ROLE_NAME,
    Permission,
)
from deerflow.persistence.orgs.model import OrganizationRow
from deerflow.persistence.release import (
    CHANNEL_DEV,
    CHANNEL_PROD,
    CHANNEL_STAGING,
)

ORG_ID = "default"


# ---------------------------------------------------------------------------
# Business-path fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'channel_router.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture
def app(sf):
    from app.gateway.routers import agent_artifacts as artifacts_router

    class _StampUserMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.state.user = SimpleNamespace(id="u-test", system_role="user")
            return await call_next(request)

    application = make_rbac_test_app(bypass_authorize=True)
    application.state.session_factory = sf
    application.add_middleware(_StampUserMiddleware)
    application.include_router(artifacts_router.router)
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


async def _seed_org(sf, *, org_id: str = ORG_ID, status: str = "active") -> None:
    async with sf() as session:
        session.add(OrganizationRow(id=org_id, slug=org_id, name=org_id, status=status))
        await session.commit()


def _make_pkg(client, *, name: str = "alpha") -> dict:
    resp = client.post("/api/v1/agent-packages", json={"name": name, "display_name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_version(
    client,
    package_id: str,
    *,
    version: str = "1.0.0",
    content: str = "hello",
    then_publish: bool = True,
) -> dict:
    body = {
        "version": version,
        "manifest": {"schema_version": "v1alpha1", "agent_entry": "main"},
        "content": content,
    }
    resp = client.post(f"/api/v1/agent-packages/{package_id}/versions", json=body)
    assert resp.status_code == 201, resp.text
    ver = resp.json()
    if then_publish:
        pub = client.post(f"/api/v1/agent-versions/{ver['id']}:publish")
        assert pub.status_code == 200, pub.text
        ver = pub.json()
    return ver


# ---------------------------------------------------------------------------
# Business path (ART-810)
# ---------------------------------------------------------------------------


class TestChannelBusiness:
    @pytest.mark.anyio
    async def test_promote_creates_channel_and_event(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        ver = _make_version(client, pkg["id"])
        resp = client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/prod:promote",
            json={"target_version_id": ver["id"], "expected_channel_version": 1},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["channel"]["current_version_id"] == ver["id"]
        assert body["channel"]["row_version"] == 2
        assert body["event"]["action"] == "promote"
        assert body["event"]["from_version_id"] is None
        assert body["event"]["to_version_id"] == ver["id"]

    @pytest.mark.anyio
    async def test_list_get_channel_after_promote(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        ver = _make_version(client, pkg["id"])
        client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/dev:promote",
            json={"target_version_id": ver["id"], "expected_channel_version": 1},
        )
        listed = client.get(f"/api/v1/agent-packages/{pkg['id']}/channels").json()
        assert len(listed) == 1
        assert listed[0]["channel"] == "dev"
        got = client.get(f"/api/v1/agent-packages/{pkg['id']}/channels/dev").json()
        assert got["current_version_id"] == ver["id"]

    @pytest.mark.anyio
    async def test_list_events(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        v1 = _make_version(client, pkg["id"], version="1.0.0")
        v2 = _make_version(client, pkg["id"], version="2.0.0", content="diff")
        client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/prod:promote",
            json={"target_version_id": v1["id"], "expected_channel_version": 1},
        )
        client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/prod:promote",
            json={"target_version_id": v2["id"], "expected_channel_version": 2},
        )
        events = client.get(f"/api/v1/agent-packages/{pkg['id']}/channels/prod/events").json()
        assert len(events) == 2
        # newest-first
        assert events[0]["to_version_id"] == v2["id"]
        assert events[1]["to_version_id"] == v1["id"]

    @pytest.mark.anyio
    async def test_rollback(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        v1 = _make_version(client, pkg["id"], version="1.0.0")
        v2 = _make_version(client, pkg["id"], version="2.0.0", content="diff")
        client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/prod:promote",
            json={"target_version_id": v2["id"], "expected_channel_version": 1},
        )
        resp = client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/prod:rollback",
            json={"target_version_id": v1["id"], "expected_channel_version": 2},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["channel"]["current_version_id"] == v1["id"]
        assert body["event"]["action"] == "rollback"
        assert body["event"]["from_version_id"] == v2["id"]

    @pytest.mark.anyio
    async def test_release_conflict_on_stale_expected(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        v1 = _make_version(client, pkg["id"], version="1.0.0")
        v2 = _make_version(client, pkg["id"], version="2.0.0", content="diff")
        client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/prod:promote",
            json={"target_version_id": v1["id"], "expected_channel_version": 1},
        )
        # Stale expected (1) after row_version is now 2 → 409.
        resp = client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/prod:promote",
            json={"target_version_id": v2["id"], "expected_channel_version": 1},
        )
        assert resp.status_code == 409, resp.text
        assert "release_conflict" in resp.text

    @pytest.mark.anyio
    async def test_gate_violation_prod_non_published(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        # Draft version (not published).
        ver = _make_version(client, pkg["id"], then_publish=False)
        resp = client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/prod:promote",
            json={"target_version_id": ver["id"], "expected_channel_version": 1},
        )
        assert resp.status_code == 409, resp.text
        assert "release_gate_violation" in resp.text

    @pytest.mark.anyio
    async def test_unknown_channel_is_404(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        resp = client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/qa:promote",
            json={"target_version_id": "v-x", "expected_channel_version": 1},
        )
        assert resp.status_code == 404, resp.text

    @pytest.mark.anyio
    async def test_missing_package_is_404(self, sf, client):
        await _seed_org(sf)
        resp = client.post(
            "/api/v1/agent-packages/pkg-missing/channels/dev:promote",
            json={"target_version_id": "v-x", "expected_channel_version": 1},
        )
        assert resp.status_code == 404, resp.text

    @pytest.mark.anyio
    async def test_target_version_not_in_package_is_404(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        other_pkg = _make_pkg(client, name="beta")
        other_ver = _make_version(client, other_pkg["id"])
        resp = client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/dev:promote",
            json={
                "target_version_id": other_ver["id"],
                "expected_channel_version": 1,
            },
        )
        assert resp.status_code == 404, resp.text

    @pytest.mark.anyio
    async def test_promote_enqueues_release_audit(self, sf, client):
        from sqlalchemy import select

        from deerflow.persistence.audit.model import AuditOutboxRow

        await _seed_org(sf)
        pkg = _make_pkg(client)
        ver = _make_version(client, pkg["id"])
        client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/prod:promote",
            json={"target_version_id": ver["id"], "expected_channel_version": 1},
        )
        async with sf() as session:
            rows = list((await session.execute(select(AuditOutboxRow))).scalars().all())
        actions = {json.loads(r.payload_json).get("action") for r in rows if r.payload_json}
        assert "release.agent.published" in actions

    @pytest.mark.anyio
    async def test_rollback_enqueues_release_audit(self, sf, client):
        from sqlalchemy import select

        from deerflow.persistence.audit.model import AuditOutboxRow

        await _seed_org(sf)
        pkg = _make_pkg(client)
        v1 = _make_version(client, pkg["id"], version="1.0.0")
        v2 = _make_version(client, pkg["id"], version="2.0.0", content="diff")
        client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/prod:promote",
            json={"target_version_id": v2["id"], "expected_channel_version": 1},
        )
        # Drain outbox so the rollback's audit row is isolated.
        async with sf() as session:
            for r in (await session.execute(select(AuditOutboxRow))).scalars().all():
                await session.delete(r)
            await session.commit()
        client.post(
            f"/api/v1/agent-packages/{pkg['id']}/channels/prod:rollback",
            json={"target_version_id": v1["id"], "expected_channel_version": 2},
        )
        async with sf() as session:
            rows = list((await session.execute(select(AuditOutboxRow))).scalars().all())
        actions = {json.loads(r.payload_json).get("action") for r in rows if r.payload_json}
        assert "release.agent.rolled_back" in actions


# ---------------------------------------------------------------------------
# RBAC matrix (ART-910) — ADR §14 + §15
# ---------------------------------------------------------------------------


def _expected_allows(role_name: str, channel: str, op: str) -> bool:
    """Oracle for the RBAC matrix.

    admin: all ops allowed on all channels.
    developer: only dev promote (carries PROMOTE_DEV, not PROMOTE/ROLLBACK).
    viewer: nothing.
    """
    perms = BUILTIN_ROLE_PERMISSIONS[role_name]
    if op == "promote":
        if channel == CHANNEL_DEV:
            return Permission.STUDIO_RELEASE_PROMOTE_DEV in perms or Permission.STUDIO_RELEASE_PROMOTE in perms
        return Permission.STUDIO_RELEASE_PROMOTE in perms
    if op == "rollback":
        return Permission.STUDIO_RELEASE_ROLLBACK in perms
    return False


@pytest.fixture
async def matrix_sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'channel_matrix.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "role_name",
    [ORG_ADMIN_ROLE_NAME, ORG_DEVELOPER_ROLE_NAME, ORG_VIEWER_ROLE_NAME],
)
@pytest.mark.parametrize("channel", [CHANNEL_DEV, CHANNEL_STAGING, CHANNEL_PROD])
async def test_rbac_matrix_promote(matrix_sf, role_name, channel):
    """§9.1 + ADR §15: developer can promote dev but not staging/prod."""
    from _router_auth_helpers import RBAC_DEFAULT_ORG_ID

    from app.gateway.routers import agent_artifacts as artifacts_router
    from deerflow.persistence.release import create_agent_package, create_agent_version, set_version_status

    await bootstrap_rbac(matrix_sf, role_name=role_name)
    # Seed a package + published version in RBAC_DEFAULT_ORG_ID (the org
    # bootstrap_rbac binds the tenant to).
    pkg = await create_agent_package(matrix_sf, org_id=RBAC_DEFAULT_ORG_ID, name="alpha", display_name="Alpha")
    ver = await create_agent_version(
        matrix_sf,
        org_id=RBAC_DEFAULT_ORG_ID,
        package_id=pkg.id,
        version="1.0.0",
        manifest={"schema_version": "v1", "agent_entry": "main"},
        content="x",
    )
    await set_version_status(matrix_sf, version_id=ver.id, org_id=RBAC_DEFAULT_ORG_ID, status="published")

    app = make_rbac_test_app(sf=matrix_sf)
    app.state.session_factory = matrix_sf
    app.include_router(artifacts_router.router)
    client = TestClient(app)

    resp = client.post(
        f"/api/v1/agent-packages/{pkg.id}/channels/{channel}:promote",
        json={"target_version_id": ver.id, "expected_channel_version": 1},
    )
    allowed = _expected_allows(role_name, channel, "promote")
    if allowed:
        # Would proceed past authorize; row_version=1 matches the implicit
        # channel create, so the promote succeeds (200) unless the gate
        # rejects (only relevant for staging/prod on a published version —
        # which is allowed, so 200).
        assert resp.status_code in (200, 409), f"{role_name} {channel}: {resp.status_code} {resp.text}"
    else:
        assert resp.status_code == 403, f"{role_name} {channel}: expected 403, got {resp.status_code}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "role_name",
    [ORG_ADMIN_ROLE_NAME, ORG_DEVELOPER_ROLE_NAME, ORG_VIEWER_ROLE_NAME],
)
async def test_rbac_matrix_rollback(matrix_sf, role_name):
    """§9.1 + ADR §15: rollback requires studio:release:rollback (admin-only)."""
    from _router_auth_helpers import RBAC_DEFAULT_ORG_ID

    from app.gateway.routers import agent_artifacts as artifacts_router
    from deerflow.persistence.release import (
        create_agent_package,
        create_agent_version,
        promote_channel,
        set_version_status,
    )

    await bootstrap_rbac(matrix_sf, role_name=role_name)
    pkg = await create_agent_package(matrix_sf, org_id=RBAC_DEFAULT_ORG_ID, name="alpha", display_name="Alpha")
    v1 = await create_agent_version(
        matrix_sf,
        org_id=RBAC_DEFAULT_ORG_ID,
        package_id=pkg.id,
        version="1.0.0",
        manifest={"schema_version": "v1", "agent_entry": "main"},
        content="x",
    )
    v2 = await create_agent_version(
        matrix_sf,
        org_id=RBAC_DEFAULT_ORG_ID,
        package_id=pkg.id,
        version="2.0.0",
        manifest={"schema_version": "v1", "agent_entry": "main"},
        content="y",
    )
    await set_version_status(matrix_sf, version_id=v1.id, org_id=RBAC_DEFAULT_ORG_ID, status="published")
    await set_version_status(matrix_sf, version_id=v2.id, org_id=RBAC_DEFAULT_ORG_ID, status="published")
    # Seed the channel at v2 so a rollback to v1 would otherwise succeed.
    await promote_channel(
        matrix_sf,
        org_id=RBAC_DEFAULT_ORG_ID,
        package_id=pkg.id,
        channel=CHANNEL_PROD,
        target_version_id=v2.id,
        expected_channel_version=1,
    )

    app = make_rbac_test_app(sf=matrix_sf)
    app.state.session_factory = matrix_sf
    app.include_router(artifacts_router.router)
    client = TestClient(app)

    resp = client.post(
        f"/api/v1/agent-packages/{pkg.id}/channels/prod:rollback",
        json={"target_version_id": v1.id, "expected_channel_version": 2},
    )
    allowed = _expected_allows(role_name, CHANNEL_PROD, "rollback")
    if allowed:
        assert resp.status_code == 200, f"{role_name}: {resp.status_code} {resp.text}"
    else:
        assert resp.status_code == 403, f"{role_name}: expected 403, got {resp.status_code}"
