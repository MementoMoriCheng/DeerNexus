"""Business-path tests for the OIDC group-mapping router (PR-036).

Drives ``app/gateway/routers/iam.py``'s PR-036 endpoints
(``/api/v1/iam/oidc-group-mappings`` + ``:preview``) end-to-end through
TestClient + ``make_rbac_test_app(bypass_authorize=True)``. Bypass mode
is correct here: the concern is handler behaviour (CRUD, rule-3
validation, dry-run preview, cross-Org 404, audit emission) — the RBAC
boundary is covered by the parametrized matrix in
``test_rbac_iam_router.py``.

IAM IDs: ``IAM-363`` series.

ADR §10 rules exercised at the router boundary:
  rule 3 — a target role carrying a system: permission is rejected at
           create/update with 400.
  rule 5 — every CRUD mutation emits an ``oidc_group_mapping_*`` event.
  preview — runs against the CALLER's identity (no user_id in the body).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _router_auth_helpers import make_rbac_test_app
from fastapi.testclient import TestClient

import deerflow.persistence.models  # noqa: F401  — register ORM
from deerflow.persistence.iam.model import RoleRow
from deerflow.persistence.orgs.model import OrganizationRow, OrgMembershipRow
from deerflow.persistence.user.model import UserRow

# Matches the autouse ``_auto_user_context`` fixture's bound tenant.
ORG_ID = "default"
OTHER_ORG_ID = "org-other"
ISSUER = "https://idp.example.com"
# The User pydantic model validates a UUID, so the test user id must be a
# real UUID (not a short literal). The seed helpers use this same id.
USER_ID = "00000000-0000-4000-8000-000000000099"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'oidc_mapping_router.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture
def app(sf):
    """Bare FastAPI app with the IAM router + the test sf on ``app.state``.

    Uses ``make_rbac_test_app``'s ``user_factory`` so the bypass-mode
    ``_StubRbacMiddleware`` stamps a DETERMINISTIC user id (``u-test``).
    The preview endpoint resolves the caller's identity via
    ``_actor_id(request)`` and the engine then looks up that user's active
    membership — so the request user MUST match the seeded membership's
    ``user_id``. A separate ``_StampUserMiddleware`` would be overwritten
    by ``_StubRbacMiddleware`` (LIFO ordering), hence the factory hook.
    """
    from app.gateway.auth.models import User
    from app.gateway.routers import iam as iam_router

    def _fixed_user() -> User:
        return User(
            email="u-test@example.com",
            password_hash="x",
            system_role="user",
            id=USER_ID,
        )

    application = make_rbac_test_app(bypass_authorize=True, user_factory=_fixed_user)
    application.state.session_factory = sf
    application.include_router(iam_router.router)
    return application


async def _seed_world(sf, *, with_membership: bool = True) -> str:
    """Seed org + role + (optionally) the test user's active membership.

    Returns the role_id the mappings will target. The user id matches
    ``USER_ID`` (a real UUID) so the bypass-mode stub user set by
    ``make_rbac_test_app(user_factory=...)`` lines up with the seeded
    membership the preview engine resolves.
    """
    async with sf() as session:
        session.add(OrganizationRow(id=ORG_ID, slug=ORG_ID, name=ORG_ID, status="active"))
        session.add(OrganizationRow(id=OTHER_ORG_ID, slug=OTHER_ORG_ID, name=OTHER_ORG_ID, status="active"))
        session.add(UserRow(id=USER_ID, email="u-test@example.com", system_role="user"))
        await session.commit()
    if with_membership:
        async with sf() as session:
            session.add(OrgMembershipRow(id="m-default-u-test", org_id=ORG_ID, user_id=USER_ID, status="active"))
            await session.commit()
    role_id = "r-admin"
    async with sf() as session:
        session.add(RoleRow(id=role_id, org_id=ORG_ID, name="org:admin", permissions=[]))
        await session.commit()
    return role_id


def _create_body(
    *,
    issuer: str = ISSUER,
    group_value: str = "admins",
    target_org_id: str = ORG_ID,
    target_role_id: str = "r-admin",
    mode: str = "additive",
) -> dict:
    return {
        "issuer": issuer,
        "group_claim": "groups",
        "group_value": group_value,
        "target_org_id": target_org_id,
        "target_role_id": target_role_id,
        "mode": mode,
    }


# ===========================================================================
# IAM-363a — CRUD happy path
# ===========================================================================


class TestCrud:
    @pytest.mark.anyio
    async def test_create_then_list_then_update_then_delete(self, sf, app):
        role_id = await _seed_world(sf)
        with TestClient(app) as client:
            # Create
            resp = client.post("/api/v1/iam/oidc-group-mappings", json=_create_body(target_role_id=role_id))
            assert resp.status_code == 201, resp.text
            created = resp.json()
            assert created["mode"] == "additive"
            mapping_id = created["id"]

            # List
            resp = client.get("/api/v1/iam/oidc-group-mappings")
            assert resp.status_code == 200
            assert [m["id"] for m in resp.json()] == [mapping_id]

            # Update
            resp = client.patch(f"/api/v1/iam/oidc-group-mappings/{mapping_id}", json={"group_value": "ops", "description": "renamed"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["group_value"] == "ops"
            assert resp.json()["description"] == "renamed"

            # Delete
            resp = client.delete(f"/api/v1/iam/oidc-group-mappings/{mapping_id}")
            assert resp.status_code == 204
            # List is now empty.
            resp = client.get("/api/v1/iam/oidc-group-mappings")
            assert resp.json() == []

    @pytest.mark.anyio
    async def test_create_cross_org_target_rejected(self, sf, app):
        """An admin may only target their OWN org (no cross-Org injection)."""
        await _seed_world(sf)
        with TestClient(app) as client:
            resp = client.post("/api/v1/iam/oidc-group-mappings", json=_create_body(target_org_id=OTHER_ORG_ID))
            assert resp.status_code == 400
            assert "active org" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_duplicate_allowlist_entry_409(self, sf, app):
        role_id = await _seed_world(sf)
        with TestClient(app) as client:
            client.post("/api/v1/iam/oidc-group-mappings", json=_create_body(target_role_id=role_id))
            resp = client.post("/api/v1/iam/oidc-group-mappings", json=_create_body(target_role_id=role_id))
            assert resp.status_code == 409


# ===========================================================================
# IAM-363b — rule 3 validation (no system permissions target)
# ===========================================================================


class TestRule3Validation:
    @pytest.mark.anyio
    async def test_create_unknown_role_id_rejected(self, sf, app):
        await _seed_world(sf)
        with TestClient(app) as client:
            resp = client.post("/api/v1/iam/oidc-group-mappings", json=_create_body(target_role_id="does-not-exist"))
            assert resp.status_code == 400
            assert "does not reference a known role" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_create_system_role_target_rejected(self, sf, app):
        await _seed_world(sf)
        # A system-template role carrying a system: permission.
        async with sf() as session:
            session.add(
                RoleRow(
                    id="r-sys",
                    org_id=None,
                    name="system:admin",
                    is_system=True,
                    permissions=["system:org:operate_all"],
                )
            )
            await session.commit()
        with TestClient(app) as client:
            resp = client.post("/api/v1/iam/oidc-group-mappings", json=_create_body(target_role_id="r-sys"))
            assert resp.status_code == 400
            assert "system" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_update_to_system_role_rejected(self, sf, app):
        role_id = await _seed_world(sf)
        async with sf() as session:
            session.add(RoleRow(id="r-sys", org_id=None, name="system:admin", is_system=True, permissions=["system:org:operate_all"]))
            await session.commit()
        with TestClient(app) as client:
            created = client.post("/api/v1/iam/oidc-group-mappings", json=_create_body(target_role_id=role_id)).json()
            resp = client.patch(f"/api/v1/iam/oidc-group-mappings/{created['id']}", json={"target_role_id": "r-sys"})
            assert resp.status_code == 400


# ===========================================================================
# IAM-363c — cross-Org existence-hiding (404)
# ===========================================================================


class TestCrossOrg404:
    @pytest.mark.anyio
    async def test_update_wrong_org_404(self, sf, app):
        """A mapping targeting another org is invisible (404, not 403)."""
        await _seed_world(sf)
        from deerflow.persistence.iam.repository import create_oidc_group_mapping

        # Seed a mapping whose target_org_id is OTHER_ORG_ID (not the caller's ORG_ID).
        await create_oidc_group_mapping(
            sf,
            issuer=ISSUER,
            group_claim="groups",
            group_value="admins",
            target_org_id=OTHER_ORG_ID,
            target_role_id="r-admin",
        )
        # We need a role in OTHER_ORG_ID for the seed to be valid; seed directly via raw row.
        async with sf() as session:
            session.add(RoleRow(id="r-other", org_id=OTHER_ORG_ID, name="org:admin", permissions=[]))
            await session.commit()
        # Re-seed now that the role exists.
        mapping = await create_oidc_group_mapping(
            sf,
            issuer=ISSUER,
            group_claim="groups",
            group_value="admins",
            target_org_id=OTHER_ORG_ID,
            target_role_id="r-other",
        )
        with TestClient(app) as client:
            resp = client.patch(f"/api/v1/iam/oidc-group-mappings/{mapping.id}", json={"group_value": "ops"})
            assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_delete_wrong_org_404(self, sf, app):
        await _seed_world(sf)
        from deerflow.persistence.iam.repository import create_oidc_group_mapping

        async with sf() as session:
            session.add(RoleRow(id="r-other", org_id=OTHER_ORG_ID, name="org:admin", permissions=[]))
            await session.commit()
        mapping = await create_oidc_group_mapping(
            sf,
            issuer=ISSUER,
            group_claim="groups",
            group_value="admins",
            target_org_id=OTHER_ORG_ID,
            target_role_id="r-other",
        )
        with TestClient(app) as client:
            resp = client.delete(f"/api/v1/iam/oidc-group-mappings/{mapping.id}")
            assert resp.status_code == 404


# ===========================================================================
# IAM-363d — dry-run preview (runs against the caller)
# ===========================================================================


class TestPreview:
    @pytest.mark.anyio
    async def test_preview_returns_planned_no_writes(self, sf, app):
        role_id = await _seed_world(sf)
        # Seed a mapping rule the caller would match.
        from deerflow.persistence.iam.repository import create_oidc_group_mapping

        await create_oidc_group_mapping(
            sf,
            issuer=ISSUER,
            group_claim="groups",
            group_value="admins",
            target_org_id=ORG_ID,
            target_role_id=role_id,
        )
        with TestClient(app) as client:
            resp = client.post("/api/v1/iam/oidc-group-mappings:preview", json={"issuer": ISSUER, "groups": ["admins"]})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["dry_run"] is True
            assert len(body["planned"]) == 1
            assert body["applied"] == []
            assert body["planned"][0]["group_value"] == "admins"

        # No binding was written (dry-run).
        from deerflow.persistence.iam.repository import list_role_bindings

        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert bindings == []

    @pytest.mark.anyio
    async def test_preview_no_matching_group_empty_planned(self, sf, app):
        await _seed_world(sf)
        with TestClient(app) as client:
            resp = client.post("/api/v1/iam/oidc-group-mappings:preview", json={"issuer": ISSUER, "groups": ["nonexistent"]})
            assert resp.status_code == 200
            assert resp.json()["planned"] == []


# ===========================================================================
# IAM-363e — audit emission (rule 5)
# ===========================================================================


class TestAuditEmission:
    @pytest.mark.anyio
    async def test_create_enqueues_created_audit_event(self, sf, app):
        """PR-042: the create path enqueues a Class A audit row (ADR §7.1) with
        the normalized ``iam.oidc_group_mapping.created`` action."""
        from sqlalchemy import select

        from deerflow.contracts.events import AuditEvent
        from deerflow.persistence.audit.model import AuditOutboxRow

        role_id = await _seed_world(sf)
        with TestClient(app) as client:
            resp = client.post("/api/v1/iam/oidc-group-mappings", json=_create_body(target_role_id=role_id))
            assert resp.status_code == 201
        async with sf() as session:
            rows = (await session.execute(select(AuditOutboxRow).where(AuditOutboxRow.org_id == ORG_ID))).scalars().all()
        actions = {AuditEvent.model_validate_json(r.payload_json).action for r in rows}
        assert "iam.oidc_group_mapping.created" in actions
