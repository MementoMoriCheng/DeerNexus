"""Business + RBAC tests for the catalog router (PR-054).

Drives ``app.gateway.routers.catalog`` through TestClient: ``GET /orgs/{org}/catalog``
with filters, cross-Org path-mismatch enforcement, and the RBAC matrix
(admin allows; developer/viewer deny — STUDIO_PACKAGE_READ is admin-only per
the PR-030 registry pin).

Mirrors ``test_agent_artifact_router.py`` conventions. The catalog write path
lands in a follow-up, so the empty-Organ case (no seeded rows) returns ``[]``.

Catalog IDs: ``ART-1300`` (router business) / ``ART-1400`` (router RBAC).
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from deerflow.persistence.catalog import (
    CATALOG_STATUS_ARCHIVED,
    RESOURCE_TYPE_AGENT,
    RESOURCE_TYPE_SKILL,
    SOURCE_DATABASE,
    CatalogEntryRow,
)
from deerflow.persistence.orgs.model import OrganizationRow

ORG_ID = "default"


# ---------------------------------------------------------------------------
# Business-path fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'catalog_router.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture
def app(sf):
    from app.gateway.routers import catalog as catalog_router

    class _StampUserMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.state.user = SimpleNamespace(id="u-test", system_role="user")
            return await call_next(request)

    application = make_rbac_test_app(bypass_authorize=True)
    application.state.session_factory = sf
    application.add_middleware(_StampUserMiddleware)
    application.include_router(catalog_router.router)
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


async def _seed_org(sf, *, org_id: str = ORG_ID) -> None:
    async with sf() as session:
        session.add(OrganizationRow(id=org_id, slug=org_id, name=org_id, status="active"))
        await session.commit()


async def _seed_entry(
    sf,
    *,
    entry_id: str,
    org_id: str = ORG_ID,
    workspace_id: str | None = None,
    resource_type: str = RESOURCE_TYPE_AGENT,
    resource_id: str | None = None,
    name: str = "n",
    source: str = SOURCE_DATABASE,
    status: str = "active",
) -> None:
    async with sf() as session:
        session.add(
            CatalogEntryRow(
                id=entry_id,
                org_id=org_id,
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id or f"res-{entry_id}",
                name=name,
                display_name=name,
                source=source,
                status=status,
                metadata_={"k": "v"},
                synced_at=datetime.now(UTC),
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Business path (ART-1300)
# ---------------------------------------------------------------------------


class TestCatalogBusiness:
    @pytest.mark.anyio
    async def test_empty_org_returns_empty_list(self, sf, client):
        await _seed_org(sf)
        resp = client.get(f"/api/v1/orgs/{ORG_ID}/catalog")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    @pytest.mark.anyio
    async def test_lists_entries(self, sf, client):
        await _seed_org(sf)
        await _seed_entry(sf, entry_id="a", resource_type=RESOURCE_TYPE_AGENT, name="alpha")
        await _seed_entry(sf, entry_id="b", resource_type=RESOURCE_TYPE_SKILL, name="beta")
        resp = client.get(f"/api/v1/orgs/{ORG_ID}/catalog")
        assert resp.status_code == 200
        body = resp.json()
        ids = {e["id"] for e in body}
        assert ids == {"a", "b"}
        # metadata field is exposed under the documented name.
        assert all("metadata" in e for e in body)

    @pytest.mark.anyio
    async def test_resource_type_filter(self, sf, client):
        await _seed_org(sf)
        await _seed_entry(sf, entry_id="a", resource_type=RESOURCE_TYPE_AGENT)
        await _seed_entry(sf, entry_id="b", resource_type=RESOURCE_TYPE_SKILL)
        resp = client.get(f"/api/v1/orgs/{ORG_ID}/catalog?resource_type=agent")
        assert resp.status_code == 200
        assert {e["id"] for e in resp.json()} == {"a"}

    @pytest.mark.anyio
    async def test_workspace_filter(self, sf, client):
        await _seed_org(sf)
        await _seed_entry(sf, entry_id="ws1", workspace_id="ws-1")
        await _seed_entry(sf, entry_id="ws2", workspace_id="ws-2")
        resp = client.get(f"/api/v1/orgs/{ORG_ID}/catalog?workspace_id=ws-1")
        assert resp.status_code == 200
        assert {e["id"] for e in resp.json()} == {"ws1"}

    @pytest.mark.anyio
    async def test_default_excludes_archived(self, sf, client):
        await _seed_org(sf)
        await _seed_entry(sf, entry_id="act", status="active")
        await _seed_entry(sf, entry_id="arc", status=CATALOG_STATUS_ARCHIVED)
        resp = client.get(f"/api/v1/orgs/{ORG_ID}/catalog")
        assert {e["id"] for e in resp.json()} == {"act"}

    @pytest.mark.anyio
    async def test_path_org_mismatch_is_403(self, sf, client):
        """The path org_id must equal the bound tenant org_id; a mismatch is
        cross-Org probing → 403 (not 404, to distinguish from a missing
        resource within the caller's own Org)."""
        await _seed_org(sf)
        resp = client.get("/api/v1/orgs/other-org/catalog")
        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# RBAC matrix (ART-1400) — STUDIO_PACKAGE_READ is admin-only
# ---------------------------------------------------------------------------


def _expect_allows(role_name: str) -> bool:
    return Permission.STUDIO_PACKAGE_READ in BUILTIN_ROLE_PERMISSIONS[role_name]


@pytest.fixture
async def matrix_sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'catalog_matrix.db'}"
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
async def test_rbac_matrix_catalog_read(matrix_sf, role_name):
    """§9.1: studio:package:read (which gates catalog) is admin-only."""
    from _router_auth_helpers import RBAC_DEFAULT_ORG_ID

    from app.gateway.routers import catalog as catalog_router

    await bootstrap_rbac(matrix_sf, role_name=role_name)
    app = make_rbac_test_app(sf=matrix_sf)
    app.state.session_factory = matrix_sf
    app.include_router(catalog_router.router)
    client = TestClient(app)

    resp = client.get(f"/api/v1/orgs/{RBAC_DEFAULT_ORG_ID}/catalog")
    if _expect_allows(role_name):
        assert resp.status_code == 200, f"{role_name}: {resp.status_code} {resp.text}"
    else:
        assert resp.status_code == 403, f"{role_name}: expected 403, got {resp.status_code}"
