"""Business-path tests for the OrgMembership lifecycle router (PR-037).

Drives ``app/gateway/routers/iam.py``'s PR-037 endpoints
(``/api/v1/iam/org-memberships/{user_id}:suspend`` / ``:activate`` / ``GET``)
end-to-end through TestClient + ``make_rbac_test_app(bypass_authorize=True)``.
Bypass mode: the concern is handler behaviour (status transition, last-admin
guard, cache invalidation, audit emission), not the RBAC boundary.

IAM IDs: ``IAM-371`` series.

ADR §7 + §11 under test:
  - suspend active→suspended (the revocation).
  - suspend of the sole org:admin → 409 (last-admin guard).
  - activate suspended→active.
  - cache invalidated post-commit (so the next request sees the change).
  - cross-Org → 404 (existence-hiding).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from _router_auth_helpers import make_rbac_test_app
from fastapi.testclient import TestClient

import deerflow.persistence.models  # noqa: F401  — register ORM
from deerflow.persistence.iam.model import RoleBindingRow, RoleRow
from deerflow.persistence.orgs.model import OrganizationRow, OrgMembershipRow
from deerflow.persistence.user.model import UserRow

# Matches the autouse ``_auto_user_context`` fixture's bound tenant.
ORG_ID = "default"
OTHER_ORG_ID = "org-other"
ISSUER = "https://idp.example.com"
# The User pydantic model validates a UUID, so the test caller id is real.
CALLER_ID = "00000000-0000-4000-8000-0000000000c1"
TARGET_USER_ID = "00000000-0000-4000-8000-0000000000c2"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'membership_router.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture
def app(sf):
    """Bare FastAPI app with the IAM router + the test sf on ``app.state``.

    Uses ``make_rbac_test_app``'s ``user_factory`` so the bypass-mode stub
    stamps a deterministic caller id (``CALLER_ID``); the router reads the
    caller off ``_actor_id`` / the bound TenantContext org.
    """
    from app.gateway.auth.models import User
    from app.gateway.routers import iam as iam_router

    def _caller() -> User:
        return User(email="caller@example.com", password_hash="x", system_role="user", id=CALLER_ID)

    application = make_rbac_test_app(bypass_authorize=True, user_factory=_caller)
    application.state.session_factory = sf
    application.include_router(iam_router.router)
    return application


async def _seed_world(sf, *, target_admin: bool = False) -> str:
    """Seed org + builtin roles + caller membership + caller org:admin binding.

    Returns the org:admin role id. When ``target_admin`` is True the target
    user also gets an org:admin binding (so it is a second admin, not sole).
    """
    from sqlalchemy import select

    from deerflow.tenancy import ensure_builtin_roles

    await ensure_builtin_roles(sf)
    async with sf() as session:
        session.add(OrganizationRow(id=ORG_ID, slug=ORG_ID, name=ORG_ID, status="active"))
        session.add(OrganizationRow(id=OTHER_ORG_ID, slug=OTHER_ORG_ID, name=OTHER_ORG_ID, status="active"))
        session.add(UserRow(id=CALLER_ID, email="caller@example.com", system_role="user"))
        session.add(UserRow(id=TARGET_USER_ID, email="target@example.com", system_role="user"))
        await session.commit()
    async with sf() as session:
        session.add(OrgMembershipRow(id="m-caller", org_id=ORG_ID, user_id=CALLER_ID, status="active"))
        session.add(OrgMembershipRow(id="m-target", org_id=ORG_ID, user_id=TARGET_USER_ID, status="active"))
        await session.commit()
    async with sf() as session:
        role = (await session.execute(select(RoleRow).where(RoleRow.name == "org:admin", RoleRow.is_system.is_(True)))).scalar_one()
        role_id = role.id
    # Caller is the sole org:admin.
    async with sf() as session:
        session.add(
            RoleBindingRow(
                id="b-caller",
                org_id=ORG_ID,
                principal_type="user",
                principal_id=CALLER_ID,
                role_id=role_id,
            )
        )
        if target_admin:
            session.add(
                RoleBindingRow(
                    id="b-target",
                    org_id=ORG_ID,
                    principal_type="user",
                    principal_id=TARGET_USER_ID,
                    role_id=role_id,
                )
            )
        await session.commit()
    return role_id


# ===========================================================================
# IAM-371a — suspend / activate lifecycle
# ===========================================================================


class TestSuspendActivate:
    @pytest.mark.anyio
    async def test_get_then_suspend_then_get(self, sf, app):
        await _seed_world(sf)
        with TestClient(app) as client:
            # GET — active.
            resp = client.get(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "active"

            # Suspend.
            resp = client.post(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}:suspend")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "suspended"

            # GET — suspended.
            resp = client.get(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}")
            assert resp.json()["status"] == "suspended"

    @pytest.mark.anyio
    async def test_suspend_then_activate_restores(self, sf, app):
        await _seed_world(sf)
        with TestClient(app) as client:
            client.post(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}:suspend")
            resp = client.post(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}:activate")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "active"

    @pytest.mark.anyio
    async def test_suspend_idempotent_on_already_suspended(self, sf, app):
        await _seed_world(sf)
        with TestClient(app) as client:
            client.post(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}:suspend")
            resp = client.post(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}:suspend")
            assert resp.status_code == 200
            assert resp.json()["status"] == "suspended"

    @pytest.mark.anyio
    async def test_suspend_missing_membership_404(self, sf, app):
        """A user with no membership row → 404 (existence-hiding)."""
        async with sf() as session:
            session.add(OrganizationRow(id=ORG_ID, slug=ORG_ID, name=ORG_ID, status="active"))
            await session.commit()
        with TestClient(app) as client:
            resp = client.post(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}:suspend")
            assert resp.status_code == 404


# ===========================================================================
# IAM-371b — last-admin guard on suspend
# ===========================================================================


class TestLastAdminGuard:
    @pytest.mark.anyio
    async def test_suspend_sole_admin_refused_409(self, sf, app):
        """Suspending the sole org:admin (the caller) → 409 (ADR §7)."""
        # Caller is the sole admin; suspend the CALLER. _seed_world already
        # bound only the caller. (Suspending a non-admin target is fine —
        # only the sole-admin path is guarded.)
        await _seed_world(sf)
        with TestClient(app) as client:
            # Suspend the caller themselves (the sole admin).
            resp = client.post(f"/api/v1/iam/org-memberships/{CALLER_ID}:suspend")
            assert resp.status_code == 409
            assert "last" in resp.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_suspend_when_two_admins_permitted(self, sf, app):
        """Two admins → suspending one is permitted (a second remains)."""
        await _seed_world(sf, target_admin=True)
        with TestClient(app) as client:
            resp = client.post(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}:suspend")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "suspended"

    @pytest.mark.anyio
    async def test_suspend_non_admin_not_guarded(self, sf, app):
        """Suspending a user with no org:admin binding is never last-admin-guarded."""
        # Target has a membership but no admin binding; caller is sole admin.
        await _seed_world(sf)
        with TestClient(app) as client:
            resp = client.post(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}:suspend")
            assert resp.status_code == 200


# ===========================================================================
# IAM-371c — cache invalidation post-commit (ADR §11)
# ===========================================================================


class TestCacheInvalidation:
    @pytest.mark.anyio
    async def test_suspend_calls_invalidate_principal(self, sf, app):
        await _seed_world(sf)
        from unittest.mock import MagicMock

        mock_svc = MagicMock()
        with patch("app.gateway.routers.iam.get_authorize_service", return_value=mock_svc):
            with TestClient(app) as client:
                resp = client.post(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}:suspend")
                assert resp.status_code == 200
        # The router MUST have invalidated the target user's cache.
        invalidations = mock_svc.invalidate_principal.call_args_list
        assert any(c.kwargs.get("principal_type") == "user" and c.kwargs.get("principal_id") == TARGET_USER_ID for c in invalidations), f"target user not invalidated: {invalidations}"


# ===========================================================================
# IAM-371d — audit emission
# ===========================================================================


class TestAuditEmission:
    @pytest.mark.anyio
    async def test_suspend_enqueues_audit_event(self, sf, app):
        """PR-042: suspend enqueues a Class A ``iam.membership.suspended``
        audit row in the same transaction as the status flip (ADR §7.1)."""
        from sqlalchemy import select

        from deerflow.contracts.events import AuditEvent
        from deerflow.persistence.audit.model import AuditOutboxRow

        await _seed_world(sf)
        with TestClient(app) as client:
            resp = client.post(f"/api/v1/iam/org-memberships/{TARGET_USER_ID}:suspend")
            assert resp.status_code == 200
        async with sf() as session:
            rows = (await session.execute(select(AuditOutboxRow).where(AuditOutboxRow.org_id == ORG_ID))).scalars().all()
        actions = {AuditEvent.model_validate_json(r.payload_json).action for r in rows}
        assert "iam.membership.suspended" in actions
