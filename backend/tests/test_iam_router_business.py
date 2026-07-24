"""Business-path tests for the IAM ServiceAccount router (PR-034).

Drives ``app/gateway/routers/iam.py`` end-to-end through TestClient +
``make_rbac_test_app(bypass_authorize=True)``. Bypass mode is the right
choice here because these tests are about the *handler* behaviour
(lifecycle transitions, audit emission, cross-Org 404, cache
invalidation) — the RBAC boundary is exhaustively covered in
``test_rbac_iam_router.py``.

Org id: the autouse ``_auto_user_context`` fixture in ``conftest.py``
binds a TenantContext for ``org_id="default"``. We seed against that
same Org so the router's ``_require_org_id`` and the seed line up
without any extra middleware.

The router calls ``_sf(request)`` which prefers
``request.app.state.session_factory`` (so the test factory is used)
and falls back to ``get_session_factory()``. We seed the factory
explicitly on ``app.state`` so the router reads / writes through the
isolated SQLite the assertions inspect.

IAM IDs: ``IAM-31x`` (router business path; matrix is IAM-21x,
repository is IAM-30x, authorize path is IAM-22x).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from _router_auth_helpers import make_rbac_test_app
from fastapi.testclient import TestClient

import deerflow.persistence.models  # noqa: F401  — register ORM
from deerflow.persistence.iam.model import ServiceAccountRow
from deerflow.persistence.orgs.model import OrganizationRow

# Matches the autouse ``_auto_user_context`` fixture's bound tenant.
ORG_ID = "default"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'iam_router.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture
def app(sf):
    """Bare FastAPI app with the IAM router + the test sf on ``app.state``.

    Bypass mode (no DB-backed authorize) is correct for these tests:
    the concern is handler behaviour, not the RBAC decision. The
    autouse ``_auto_user_context`` fixture already bound a
    TenantContext for ``ORG_ID``, so no extra tenant-binding middleware
    is needed. ``app.state.user`` is stamped via a tiny middleware
    because ``_actor_id`` reads it for audit attribution.
    """
    from fastapi import Request
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.gateway.routers import iam as iam_router

    class _StampUserMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.state.user = SimpleNamespace(id="u-test", system_role="user")
            return await call_next(request)

    application = make_rbac_test_app(bypass_authorize=True)
    application.state.session_factory = sf
    # Stamp middleware must run AFTER _StubRbacMiddleware (added by
    # make_rbac_test_app) so it sees the rebuilt Request — Starlette
    # executes middleware in LIFO order on the inbound leg, so adding
    # it second means it runs first; that's wrong here. We instead add
    # it BEFORE the router layer via a second add_middleware call,
    # which Starlette interprets as innermost. Pragma-difference: the
    # bypass flag is set on request.state by _StubRbacMiddleware, so
    # both middlewares see the same Request scope.
    application.add_middleware(_StampUserMiddleware)
    application.include_router(iam_router.router)
    return application


async def _seed_org(sf, *, org_id: str = ORG_ID, status: str = "active") -> None:
    async with sf() as session:
        session.add(OrganizationRow(id=org_id, slug=org_id, name=org_id, status=status))
        await session.commit()


async def _seed_role(sf, *, name: str = "org:admin", is_system: bool = True) -> str:
    """Seed one role row and return its id (FK target for binding)."""
    import uuid

    from deerflow.persistence.iam.model import RoleRow

    role_id = uuid.uuid4().hex
    async with sf() as session:
        session.add(RoleRow(id=role_id, org_id=None if is_system else ORG_ID, name=name, is_system=is_system, permissions=[]))
        await session.commit()
    return role_id


# ===========================================================================
# IAM-310 — happy-path lifecycle
# ===========================================================================


class TestServiceAccountLifecycle:
    @pytest.mark.anyio
    async def test_create_get_list_update_disable_enable_delete(self, sf, app):
        await _seed_org(sf)
        with TestClient(app) as client:
            # Create
            resp = client.post(
                "/api/v1/iam/service-accounts",
                json={"name": "ci-runner", "purpose": "ci", "environment": "prod"},
            )
            assert resp.status_code == 201, resp.text
            sa = resp.json()
            sa_id = sa["id"]
            assert sa["status"] == "active"
            assert sa["purpose"] == "ci"

            # Get
            resp = client.get(f"/api/v1/iam/service-accounts/{sa_id}")
            assert resp.status_code == 200
            assert resp.json()["name"] == "ci-runner"

            # List
            resp = client.get("/api/v1/iam/service-accounts")
            assert resp.status_code == 200
            assert any(r["id"] == sa_id for r in resp.json())

            # Patch
            resp = client.patch(
                f"/api/v1/iam/service-accounts/{sa_id}",
                json={"description": "updated", "environment": "staging"},
            )
            assert resp.status_code == 200, resp.text
            updated = resp.json()
            assert updated["description"] == "updated"
            assert updated["environment"] == "staging"

            # Disable
            resp = client.post(f"/api/v1/iam/service-accounts/{sa_id}:disable")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "disabled"

            # Enable
            resp = client.post(f"/api/v1/iam/service-accounts/{sa_id}:enable")
            assert resp.status_code == 200
            assert resp.json()["status"] == "active"

            # Delete
            resp = client.delete(f"/api/v1/iam/service-accounts/{sa_id}")
            assert resp.status_code == 204

            # Get → 404
            resp = client.get(f"/api/v1/iam/service-accounts/{sa_id}")
            assert resp.status_code == 404


# ===========================================================================
# IAM-311 — cross-Org isolation
# ===========================================================================


class TestCrossOrgIsolation:
    @pytest.mark.anyio
    async def test_get_sa_in_other_org_returns_404(self, sf, app):
        """Existence-hiding: cross-Org access returns 404 not 403."""
        await _seed_org(sf)
        await _seed_org(sf, org_id="org-other")
        # Seed a SA in org-other directly via the ORM.
        async with sf() as session:
            session.add(ServiceAccountRow(id="sa-foreign", org_id="org-other", name="foreign-bot", status="active"))
            await session.commit()

        # The bound tenant context is org-test; ask for the foreign SA.
        with TestClient(app) as client:
            resp = client.get("/api/v1/iam/service-accounts/sa-foreign")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_list_excludes_other_org_sas(self, sf, app):
        await _seed_org(sf)
        await _seed_org(sf, org_id="org-other")
        async with sf() as session:
            session.add(ServiceAccountRow(id="sa-local", org_id=ORG_ID, name="local-bot", status="active"))
            session.add(ServiceAccountRow(id="sa-foreign", org_id="org-other", name="foreign-bot", status="active"))
            await session.commit()

        with TestClient(app) as client:
            resp = client.get("/api/v1/iam/service-accounts")
        assert resp.status_code == 200
        names = {r["name"] for r in resp.json()}
        assert "local-bot" in names
        assert "foreign-bot" not in names


# ===========================================================================
# IAM-312 — error paths
# ===========================================================================


class TestErrorPaths:
    @pytest.mark.anyio
    async def test_create_duplicate_name_returns_409(self, sf, app):
        await _seed_org(sf)
        with TestClient(app) as client:
            resp = client.post("/api/v1/iam/service-accounts", json={"name": "bot"})
            assert resp.status_code == 201
            resp = client.post("/api/v1/iam/service-accounts", json={"name": "bot"})
            assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_get_missing_returns_404(self, sf, app):
        await _seed_org(sf)
        with TestClient(app) as client:
            resp = client.get("/api/v1/iam/service-accounts/never-existed")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_patch_missing_returns_404(self, sf, app):
        await _seed_org(sf)
        with TestClient(app) as client:
            resp = client.patch("/api/v1/iam/service-accounts/never-existed", json={"description": "x"})
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_delete_missing_returns_404(self, sf, app):
        await _seed_org(sf)
        with TestClient(app) as client:
            resp = client.delete("/api/v1/iam/service-accounts/never-existed")
        assert resp.status_code == 404


# ===========================================================================
# IAM-313 — role binding lifecycle + delete cascade
# ===========================================================================


class TestRoleBindingLifecycle:
    @pytest.mark.anyio
    async def test_create_list_delete_binding(self, sf, app):
        await _seed_org(sf)
        role_id = await _seed_role(sf)
        with TestClient(app) as client:
            sa = client.post("/api/v1/iam/service-accounts", json={"name": "bot"}).json()

            # Create binding
            resp = client.post(
                f"/api/v1/iam/service-accounts/{sa['id']}/role-bindings",
                json={"role_id": role_id},
            )
            assert resp.status_code == 201, resp.text
            binding = resp.json()
            assert binding["role_id"] == role_id

            # List bindings
            resp = client.get(f"/api/v1/iam/service-accounts/{sa['id']}/role-bindings")
            assert resp.status_code == 200
            assert len(resp.json()) == 1

            # Delete binding
            resp = client.delete(f"/api/v1/iam/service-accounts/{sa['id']}/role-bindings/{binding['id']}")
            assert resp.status_code == 204

            # List is empty
            resp = client.get(f"/api/v1/iam/service-accounts/{sa['id']}/role-bindings")
            assert resp.status_code == 200
            assert resp.json() == []

    @pytest.mark.anyio
    async def test_delete_sa_cleans_role_bindings(self, sf, app):
        """ADR §12: SA delete MUST land with binding cleanup atomically."""
        await _seed_org(sf)
        role_id = await _seed_role(sf)
        with TestClient(app) as client:
            sa = client.post("/api/v1/iam/service-accounts", json={"name": "bot"}).json()
            client.post(
                f"/api/v1/iam/service-accounts/{sa['id']}/role-bindings",
                json={"role_id": role_id},
            )

            # Delete the SA — bindings must be cleared in the same transaction.
            resp = client.delete(f"/api/v1/iam/service-accounts/{sa['id']}")
            assert resp.status_code == 204

        # Confirm at the ORM level: no role_bindings rows reference the SA.
        from sqlalchemy import select

        from deerflow.persistence.iam.model import RoleBindingRow

        async with sf() as session:
            rows = (
                (
                    await session.execute(
                        select(RoleBindingRow).where(
                            RoleBindingRow.principal_type == "service_account",
                            RoleBindingRow.principal_id == sa["id"],
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert rows == []


# ===========================================================================
# IAM-314 — audit emission + cache invalidation
# ===========================================================================


class TestAuditAndCacheInvalidation:
    @pytest.mark.anyio
    async def test_create_enqueues_audit_in_same_transaction(self, sf, app):
        """PR-042: the create path enqueues a Class A audit row in the SAME
        transaction as the ServiceAccount insert (ADR §7.1). After the 201
        commits, exactly one ``pending`` outbox row exists with the
        normalized ``iam.service_account.created`` action."""
        from sqlalchemy import select

        from deerflow.contracts.events import AuditEvent
        from deerflow.persistence.audit.model import AuditOutboxRow

        await _seed_org(sf)
        with TestClient(app) as client:
            resp = client.post("/api/v1/iam/service-accounts", json={"name": "bot"})
            assert resp.status_code == 201
        async with sf() as session:
            rows = (await session.execute(select(AuditOutboxRow).where(AuditOutboxRow.org_id == ORG_ID))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "pending"
        ev = AuditEvent.model_validate_json(rows[0].payload_json)
        assert ev.action == "iam.service_account.created"
        assert ev.outcome == "success"
        assert ev.resource is not None
        assert ev.resource.type == "service_account"
        assert ev.resource.id == resp.json()["id"]

    @pytest.mark.anyio
    async def test_disable_calls_invalidate_principal(self, sf, app):
        """Disable path must drop the SA's cached permission set (ADR §11)."""
        await _seed_org(sf)
        with patch("app.gateway.routers.iam.get_authorize_service") as mock_get_service:
            mock_service = mock_get_service.return_value
            with TestClient(app) as client:
                sa = client.post("/api/v1/iam/service-accounts", json={"name": "bot"}).json()
                mock_service.reset_mock()
                resp = client.post(f"/api/v1/iam/service-accounts/{sa['id']}:disable")
            assert resp.status_code == 200
        mock_service.invalidate_principal.assert_called_once_with(org_id=ORG_ID, principal_type="service_account", principal_id=sa["id"])

    @pytest.mark.anyio
    async def test_delete_enqueues_audit_then_invalidates(self, sf, app):
        """PR-042: delete enqueues the Class A audit row in the same
        transaction as the hard DELETE (ADR §7.1), then invalidates the
        cache POST-commit. The outbox row carries the pre-delete SA
        identity (id + name) so the audit trail survives the row's removal.
        Cache invalidation runs only after the commit succeeds."""
        from sqlalchemy import select

        from deerflow.contracts.events import AuditEvent
        from deerflow.persistence.audit.model import AuditOutboxRow

        await _seed_org(sf)
        invalidated = False
        with patch("app.gateway.routers.iam.get_authorize_service") as mock_get_service:
            mock_service = mock_get_service.return_value

            def _record_invalidate(**kwargs):
                nonlocal invalidated
                invalidated = True

            mock_service.invalidate_principal.side_effect = _record_invalidate
            with TestClient(app) as client:
                sa = client.post("/api/v1/iam/service-accounts", json={"name": "bot"}).json()
                resp = client.delete(f"/api/v1/iam/service-accounts/{sa['id']}")
                assert resp.status_code == 204
                # Invalidation runs only after the delete transaction commits.
                assert invalidated
        # The delete outbox row carries the pre-delete identity (name) and the
        # normalized action; the SA row itself is gone.
        async with sf() as session:
            from deerflow.persistence.iam.model import ServiceAccountRow

            sa_rows = (await session.execute(select(ServiceAccountRow).where(ServiceAccountRow.id == sa["id"]))).scalars().all()
            assert sa_rows == []
            outbox = (await session.execute(select(AuditOutboxRow).where(AuditOutboxRow.org_id == ORG_ID))).scalars().all()
        actions = {AuditEvent.model_validate_json(r.payload_json).action for r in outbox}
        assert "iam.service_account.deleted" in actions
