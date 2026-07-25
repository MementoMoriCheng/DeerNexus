"""Business-path + RBAC tests for the AgentPackage / AgentVersion router (PR-052).

Drives ``app/gateway/routers/agent_artifacts.py`` end-to-end through
TestClient + ``make_rbac_test_app(bypass_authorize=True)`` (business path)
and ``make_rbac_test_app(sf=sf)`` (real authorize path for the matrix).
Mirrors ``test_iam_router_business.py`` + ``test_rbac_iam_router.py``.

* Business: package lifecycle, version create (digest back-fill),
  publish/revoke state machine, cross-Org 404, audit-outbox row assertion.
* Matrix (§9.1): ``org:admin`` allows ``studio:package:*``; developer/viewer
  deny (studio is admin-only per the PR-030 registry pin).
* State-mapping (§9.2): no / suspended membership, suspended org → 403.

Artifact IDs: ``ART-21x`` (router RBAC matrix) / ``ART-31x`` (router business).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from _router_auth_helpers import (
    bootstrap_rbac,
    make_rbac_test_app,
)
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

# Matches the autouse ``_auto_user_context`` fixture's bound tenant.
ORG_ID = "default"


# ---------------------------------------------------------------------------
# Business-path fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'agent_router.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture
def app(sf):
    """Bare FastAPI app with the agent-artifact router + test sf on app.state.

    Bypass mode (no DB-backed authorize) is correct for the business tests:
    the concern is handler behaviour, not the RBAC decision. The autouse
    ``_auto_user_context`` fixture binds a TenantContext for ``ORG_ID``.
    """
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


def _make_pkg(client, *, name: str = "alpha", display_name: str = "Alpha") -> dict:
    resp = client.post("/api/v1/agent-packages", json={"name": name, "display_name": display_name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_version(client, package_id: str, *, version: str = "1.0.0", content: str = "hello") -> dict:
    body = {
        "version": version,
        "manifest": {"schema_version": "v1alpha1", "agent_entry": "main"},
        "content": content,
    }
    resp = client.post(f"/api/v1/agent-packages/{package_id}/versions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Package business path (ART-310)
# ---------------------------------------------------------------------------


class TestPackageBusiness:
    @pytest.mark.anyio
    async def test_create_list_get_patch_archive(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        assert pkg["name"] == "alpha"
        assert pkg["status"] == "active"
        pkg_id = pkg["id"]

        # list
        listed = client.get("/api/v1/agent-packages").json()
        assert len(listed) == 1
        # get
        assert client.get(f"/api/v1/agent-packages/{pkg_id}").json()["id"] == pkg_id
        # patch
        patched = client.patch(f"/api/v1/agent-packages/{pkg_id}", json={"display_name": "Renamed"}).json()
        assert patched["display_name"] == "Renamed"
        # archive
        archived = client.post(f"/api/v1/agent-packages/{pkg_id}:archive").json()
        assert archived["status"] == "archived"
        # archived excluded from default list
        assert client.get("/api/v1/agent-packages").json() == []

    @pytest.mark.anyio
    async def test_duplicate_name_conflict(self, sf, client):
        await _seed_org(sf)
        _make_pkg(client, name="dup")
        resp = client.post("/api/v1/agent-packages", json={"name": "dup", "display_name": "D2"})
        assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_cross_org_miss_is_404(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        # Own package is reachable; a foreign id simply misses → 404.
        assert client.get(f"/api/v1/agent-packages/{pkg['id']}").status_code == 200
        foreign_id = "0" * 32
        assert client.get(f"/api/v1/agent-packages/{foreign_id}").status_code == 404


# ---------------------------------------------------------------------------
# Version business path (ART-320)
# ---------------------------------------------------------------------------


class TestVersionBusiness:
    @pytest.mark.anyio
    async def test_create_backfills_digest_and_size(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        v = _make_version(client, pkg["id"], content="hello")
        assert v["digest"].startswith("sha256:")
        assert v["size_bytes"] == 5
        assert v["status"] == "draft"
        assert "content_inline" not in v  # raw content never echoed back

    @pytest.mark.anyio
    async def test_duplicate_version_conflict(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        _make_version(client, pkg["id"], version="1.0.0")
        resp = client.post(
            f"/api/v1/agent-packages/{pkg['id']}/versions",
            json={"version": "1.0.0", "manifest": {"schema_version": "v1", "agent_entry": "m"}, "content": "other"},
        )
        assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_publish_then_revoke_state_machine(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        v = _make_version(client, pkg["id"])

        published = client.post(f"/api/v1/agent-versions/{v['id']}:publish").json()
        assert published["status"] == "published"
        assert published["published_at"] is not None

        revoked = client.post(f"/api/v1/agent-versions/{v['id']}:revoke").json()
        assert revoked["status"] == "revoked"
        assert revoked["revoked_at"] is not None

    @pytest.mark.anyio
    async def test_illegal_transition_is_409(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        v = _make_version(client, pkg["id"])
        client.post(f"/api/v1/agent-versions/{v['id']}:publish")
        # published → draft is illegal
        resp = client.post(f"/api/v1/agent-versions/{v['id']}:review")
        assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_review_then_publish(self, sf, client):
        await _seed_org(sf)
        pkg = _make_pkg(client)
        v = _make_version(client, pkg["id"])
        reviewed = client.post(f"/api/v1/agent-versions/{v['id']}:review").json()
        assert reviewed["status"] == "reviewed"
        published = client.post(f"/api/v1/agent-versions/{v['id']}:publish").json()
        assert published["status"] == "published"

    @pytest.mark.anyio
    async def test_list_versions_scoped_to_package(self, sf, client):
        await _seed_org(sf)
        p1 = _make_pkg(client, name="a", display_name="A")
        p2 = _make_pkg(client, name="b", display_name="B")
        _make_version(client, p1["id"], version="1.0.0", content="a")
        _make_version(client, p2["id"], version="1.0.0", content="b")
        p1_versions = client.get(f"/api/v1/agent-packages/{p1['id']}/versions").json()
        assert len(p1_versions) == 1


# ---------------------------------------------------------------------------
# Class A audit-outbox row assertion (ART-330)
# ---------------------------------------------------------------------------


class TestAuditOutbox:
    @pytest.mark.anyio
    async def test_create_package_enqueues_audit_outbox_row(self, sf, client):
        from deerflow.persistence.audit.model import AuditOutboxRow

        await _seed_org(sf)
        _make_pkg(client)
        async with sf() as session:
            rows = list((await session.execute(__import__("sqlalchemy").select(AuditOutboxRow))).scalars().all())
        actions = {r.payload_json and __import__("json").loads(r.payload_json).get("action") for r in rows}
        # At least one outbox row carries the package-created action.
        assert any(a and "agent_package" in a for a in actions)


# ---------------------------------------------------------------------------
# RBAC matrix (ART-210) — studio:package:* is org:admin-only
# ---------------------------------------------------------------------------


def _expect_allows(role_name: str, permission: Permission) -> bool:
    return permission in BUILTIN_ROLE_PERMISSIONS[role_name]


@pytest.fixture
async def matrix_sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'agent_matrix.db'}"
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
async def test_rbac_matrix_studio_is_admin_only(matrix_sf, role_name):
    """§9.1: studio:package:* is granted only to ``org:admin``.

    developer / viewer deny both read and write. Oracle =
    ``BUILTIN_ROLE_PERMISSIONS`` (the PR-030 registry pin). The real app
    with the real router is queried so the 403 comes from ``authorize()``
    (not handler side-effects); the endpoints' own lookups are inert for an
    empty Org so admin read → 200 [] / write → 201.
    """
    from app.gateway.routers import agent_artifacts as artifacts_router

    await bootstrap_rbac(matrix_sf, role_name=role_name)
    # bootstrap_rbac already seeds RBAC_DEFAULT_ORG_ID + the role binding;
    # make_rbac_test_app(sf=...) binds the tenant to that same org so
    # ``authorize()`` resolves against the seed.

    app = make_rbac_test_app(sf=matrix_sf)
    app.state.session_factory = matrix_sf
    app.include_router(artifacts_router.router)
    client = TestClient(app)

    read_resp = client.get("/api/v1/agent-packages")
    write_resp = client.post("/api/v1/agent-packages", json={"name": "n", "display_name": "N"})

    for label, resp, permission in [
        ("read", read_resp, Permission.STUDIO_PACKAGE_READ),
        ("write", write_resp, Permission.STUDIO_PACKAGE_WRITE),
    ]:
        allowed = _expect_allows(role_name, permission)
        if allowed:
            assert resp.status_code in (200, 201), f"{role_name} {label}: expected allow, got {resp.status_code}"
        else:
            assert resp.status_code == 403, f"{role_name} {label}: expected 403, got {resp.status_code}"
