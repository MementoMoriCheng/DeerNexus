"""Business + RBAC tests for the ``POST /agent-packages:import-file`` router (PR-051).

Drives ``app.gateway.routers.agent_artifacts.import_file`` through TestClient
+ ``make_rbac_test_app(bypass_authorize=True)`` (business path) and
``make_rbac_test_app(sf=sf)`` (real authorize path for the RBAC matrix).

The import endpoint reads from the global ``get_paths().base_dir`` (the
production base_dir is operator-controlled, not request-controlled), so each
business test seeds a synthetic agent under ``tmp_path`` and patches the
global ``Paths`` singleton to that dir. The matrix test asserts 403 before
any filesystem access so it needs no fixture agent.

Mirrors ``test_agent_artifact_router.py``: ``ORG_ID`` matches the autouse
``_auto_user_context`` bound tenant; ``bootstrap_rbac`` seeds the real
``authorize()`` path for the RBAC matrix.

Artifact IDs: ``ART-41x`` (router business) / ``ART-51x`` (router RBAC).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from _router_auth_helpers import bootstrap_rbac, make_rbac_test_app
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import deerflow.persistence.models  # noqa: F401  — register ORM
from deerflow.config.agents_config import SOUL_FILENAME
from deerflow.config.paths import Paths
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'import_router.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture
def base_dir(tmp_path: Path, monkeypatch) -> Path:
    """Point the global ``Paths`` singleton at ``tmp_path`` for the import.

    The production router calls ``import_agent_from_file(base_dir=None)`` which
    resolves to ``Paths().base_dir``. Patching the singleton (and the cached
    module-global) lets the router-side call resolve into the test's hermetic
    dir without exposing a request-level ``base_dir`` field. ``Paths`` caches
    ``_base_dir`` from the constructor; replacing the singleton + the
    ``get_paths`` import in the importer module is the two-step patch.
    """
    test_paths = Paths(base_dir=tmp_path)
    import deerflow.config.paths as paths_module
    import deerflow.persistence.release.importer as importer_module

    # Both the global getter and the importer's local ``Paths`` reference must
    # resolve to the test dir. Patching ``get_paths`` covers the router's
    # ``base_dir=None`` path; patching ``Paths`` directly covers any caller
    # that constructs a fresh instance from the class.
    monkeypatch.setattr(paths_module, "_paths", test_paths)
    monkeypatch.setattr(paths_module, "get_paths", lambda: test_paths)
    monkeypatch.setattr(importer_module, "Paths", lambda base_dir=None: Paths(base_dir=tmp_path))
    return tmp_path


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


def _write_agent(base: Path, *, name: str, soul: str = "be helpful", config: dict | None = None) -> Path:
    cfg = {"name": name}
    if config:
        cfg.update(config)
    agent_dir = base / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (agent_dir / SOUL_FILENAME).write_text(soul, encoding="utf-8")
    return agent_dir


# ---------------------------------------------------------------------------
# Business path (ART-410)
# ---------------------------------------------------------------------------


class TestImportBusiness:
    @pytest.mark.anyio
    async def test_import_creates_package_and_version(self, sf, client, base_dir):
        await _seed_org(sf)
        _write_agent(base_dir, name="alpha", soul="alpha soul", config={"description": "Alpha", "model": "gpt-4o"})
        resp = client.post(
            "/api/v1/agent-packages:import-file",
            json={"name": "alpha", "version": "1.0.0"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["imported"] is True
        assert body["digest"].startswith("sha256:")
        assert body["package"]["name"] == "alpha"
        assert body["package"]["display_name"] == "alpha"
        assert body["version"]["version"] == "1.0.0"
        assert body["version"]["digest"] == body["digest"]
        assert body["source_metadata"]["source"] == "file_import"

    @pytest.mark.anyio
    async def test_import_then_list_via_get_endpoints(self, sf, client, base_dir):
        await _seed_org(sf)
        _write_agent(base_dir, name="alpha")
        post = client.post("/api/v1/agent-packages:import-file", json={"name": "alpha", "version": "1.0.0"})
        assert post.status_code == 200, post.text
        pkg_id = post.json()["package"]["id"]
        ver_id = post.json()["version"]["id"]
        # The created package/version are visible via the PR-052 read endpoints.
        assert client.get("/api/v1/agent-packages").json()[0]["id"] == pkg_id
        assert client.get(f"/api/v1/agent-packages/{pkg_id}").status_code == 200
        got = client.get(f"/api/v1/agent-versions/{ver_id}").json()
        assert got["manifest"]["schema_version"] == "1.0"
        assert got["manifest"]["agent_entry"] == "soul"

    @pytest.mark.anyio
    async def test_idempotent_reimport_returns_imported_false(self, sf, client, base_dir):
        await _seed_org(sf)
        _write_agent(base_dir, name="alpha", soul="same")
        first = client.post("/api/v1/agent-packages:import-file", json={"name": "alpha", "version": "1.0.0"})
        assert first.status_code == 200 and first.json()["imported"] is True
        second = client.post("/api/v1/agent-packages:import-file", json={"name": "alpha", "version": "1.0.0"})
        assert second.status_code == 200 and second.json()["imported"] is False
        # Same version id is returned on the dedupe hit.
        assert first.json()["version"]["id"] == second.json()["version"]["id"]

    @pytest.mark.anyio
    async def test_missing_agent_is_404(self, sf, client, base_dir):
        await _seed_org(sf)
        resp = client.post("/api/v1/agent-packages:import-file", json={"name": "ghost", "version": "1.0.0"})
        assert resp.status_code == 404, resp.text

    @pytest.mark.anyio
    async def test_bad_semver_is_422(self, sf, client, base_dir):
        await _seed_org(sf)
        _write_agent(base_dir, name="alpha")
        resp = client.post(
            "/api/v1/agent-packages:import-file",
            json={"name": "alpha", "version": "not-semver"},
        )
        assert resp.status_code == 422, resp.text

    @pytest.mark.anyio
    async def test_content_change_same_version_is_409(self, sf, client, base_dir):
        await _seed_org(sf)
        agent_dir = _write_agent(base_dir, name="alpha", soul="v1")
        first = client.post("/api/v1/agent-packages:import-file", json={"name": "alpha", "version": "1.0.0"})
        assert first.status_code == 200
        (agent_dir / SOUL_FILENAME).write_text("v2-different", encoding="utf-8")
        second = client.post("/api/v1/agent-packages:import-file", json={"name": "alpha", "version": "1.0.0"})
        assert second.status_code == 409, second.text

    @pytest.mark.anyio
    async def test_content_change_bumped_version_succeeds(self, sf, client, base_dir):
        await _seed_org(sf)
        agent_dir = _write_agent(base_dir, name="alpha", soul="v1")
        v1 = client.post("/api/v1/agent-packages:import-file", json={"name": "alpha", "version": "1.0.0"})
        assert v1.status_code == 200 and v1.json()["imported"] is True
        (agent_dir / SOUL_FILENAME).write_text("v2-different", encoding="utf-8")
        v2 = client.post("/api/v1/agent-packages:import-file", json={"name": "alpha", "version": "1.0.1"})
        assert v2.status_code == 200 and v2.json()["imported"] is True
        assert v1.json()["version"]["id"] != v2.json()["version"]["id"]
        assert v1.json()["package"]["id"] == v2.json()["package"]["id"]

    @pytest.mark.anyio
    async def test_oversized_soul_is_413(self, sf, client, base_dir):
        await _seed_org(sf)
        agent_dir = _write_agent(base_dir, name="big")
        from deerflow.persistence.release.importer import MAX_SOURCE_FILE_BYTES

        (agent_dir / SOUL_FILENAME).write_text("x" * (MAX_SOURCE_FILE_BYTES + 1), encoding="utf-8")
        resp = client.post("/api/v1/agent-packages:import-file", json={"name": "big", "version": "1.0.0"})
        assert resp.status_code == 413, resp.text

    @pytest.mark.anyio
    async def test_display_name_and_description_override(self, sf, client, base_dir):
        await _seed_org(sf)
        _write_agent(base_dir, name="alpha", config={"description": "from-file"})
        resp = client.post(
            "/api/v1/agent-packages:import-file",
            json={
                "name": "alpha",
                "version": "1.0.0",
                "display_name": "Custom Display",
                "description": "Custom desc",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["package"]["display_name"] == "Custom Display"
        assert body["package"]["description"] == "Custom desc"

    @pytest.mark.anyio
    async def test_import_then_publish_state_machine(self, sf, client, base_dir):
        """Imported Versions inherit the PR-052 lifecycle — publish/revoke work."""
        await _seed_org(sf)
        _write_agent(base_dir, name="alpha")
        post = client.post("/api/v1/agent-packages:import-file", json={"name": "alpha", "version": "1.0.0"})
        ver_id = post.json()["version"]["id"]
        # draft → published directly is legal per the ADR §4 state machine.
        pub = client.post(f"/api/v1/agent-versions/{ver_id}:publish")
        assert pub.status_code == 200
        assert pub.json()["status"] == "published"
        assert pub.json()["published_at"] is not None
        # published → revoked.
        rev = client.post(f"/api/v1/agent-versions/{ver_id}:revoke")
        assert rev.status_code == 200 and rev.json()["status"] == "revoked"


# ---------------------------------------------------------------------------
# Audit outbox (ART-420)
# ---------------------------------------------------------------------------


class TestImportAuditOutbox:
    @pytest.mark.anyio
    async def test_import_enqueues_audit_outbox_rows(self, sf, client, base_dir):
        from sqlalchemy import select

        from deerflow.persistence.audit.model import AuditOutboxRow

        await _seed_org(sf)
        _write_agent(base_dir, name="alpha")
        resp = client.post("/api/v1/agent-packages:import-file", json={"name": "alpha", "version": "1.0.0"})
        assert resp.status_code == 200, resp.text
        async with sf() as session:
            rows = list((await session.execute(select(AuditOutboxRow))).scalars().all())
        actions = {json.loads(r.payload_json).get("action") for r in rows if r.payload_json}
        assert "catalog.agent_package.imported" in actions
        assert "catalog.agent_version.imported" in actions

    @pytest.mark.anyio
    async def test_idempotent_reimport_emits_only_package_action(self, sf, client, base_dir):
        """A dedupe hit still records the package-side import attempt; the
        version action is emitted only when a new Version is created."""
        from sqlalchemy import select

        from deerflow.persistence.audit.model import AuditOutboxRow

        await _seed_org(sf)
        _write_agent(base_dir, name="alpha", soul="same")
        client.post("/api/v1/agent-packages:import-file", json={"name": "alpha", "version": "1.0.0"})
        # Drain the outbox so the second import's rows are isolated.
        async with sf() as session:
            for r in (await session.execute(select(AuditOutboxRow))).scalars().all():
                await session.delete(r)
            await session.commit()
        client.post("/api/v1/agent-packages:import-file", json={"name": "alpha", "version": "1.0.0"})
        async with sf() as session:
            rows = list((await session.execute(select(AuditOutboxRow))).scalars().all())
        actions = {json.loads(r.payload_json).get("action") for r in rows if r.payload_json}
        assert "catalog.agent_package.imported" in actions
        assert "catalog.agent_version.imported" not in actions


# ---------------------------------------------------------------------------
# RBAC matrix (ART-510) — studio:package:write gates import
# ---------------------------------------------------------------------------


def _expect_allows(role_name: str, permission: Permission) -> bool:
    return permission in BUILTIN_ROLE_PERMISSIONS[role_name]


@pytest.fixture
async def matrix_sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'import_matrix.db'}"
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
async def test_rbac_matrix_import_file_is_admin_only(matrix_sf, role_name):
    """§9.1: ``import-file`` requires ``studio:package:write`` (admin-only).

    The real ``authorize()`` path decides; developer / viewer get 403 before
    the handler runs (so no filesystem seeding is needed).
    """
    from app.gateway.routers import agent_artifacts as artifacts_router

    await bootstrap_rbac(matrix_sf, role_name=role_name)
    app = make_rbac_test_app(sf=matrix_sf)
    app.state.session_factory = matrix_sf
    app.include_router(artifacts_router.router)
    client = TestClient(app)

    resp = client.post(
        "/api/v1/agent-packages:import-file",
        json={"name": "alpha", "version": "1.0.0"},
    )
    allowed = _expect_allows(role_name, Permission.STUDIO_PACKAGE_WRITE)
    if allowed:
        # Admin would proceed past authorize; the missing agent → 404 (not 403).
        assert resp.status_code in (200, 404), f"{role_name}: expected allow, got {resp.status_code}"
    else:
        assert resp.status_code == 403, f"{role_name}: expected 403, got {resp.status_code}"
