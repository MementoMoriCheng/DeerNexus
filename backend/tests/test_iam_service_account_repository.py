"""DB CRUD tests for the IAM ServiceAccount repository (PR-034).

Covers :mod:`deerflow.persistence.iam.repository` end-to-end against an
isolated SQLite. The repository is pure data access (no audit / cache /
authz), so these tests exercise only the DB invariants — uniqueness,
CHECK constraints, cascade behaviour, Org-scoped filtering.

Fixture conventions mirror ``test_iam_authorize.py``: boot an isolated
SQLite via ``init_engine``, yield ``get_session_factory()``, tear down
with ``close_engine``.

IAM IDs: ``IAM-300`` series (repository layer; service-layer uses
``IAM-2xx`` for SA principal authorization).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.iam.model import RoleBindingRow, RoleRow
from deerflow.persistence.iam.repository import (
    SERVICE_ACCOUNT_ACTIVE,
    SERVICE_ACCOUNT_DISABLED,
    create_role_binding,
    create_service_account,
    delete_role_binding,
    delete_service_account,
    get_service_account,
    list_role_bindings,
    list_service_accounts,
    set_service_account_status,
    update_service_account,
)

ORG_ID = "org-test"
OTHER_ORG_ID = "org-other"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'iam_repo.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


async def _seed_role(sf, *, name: str = "org:admin", role_id: str = "r-1") -> str:
    """Insert one RoleRow and return its id (FK target for bindings)."""
    async with sf() as session:
        session.add(RoleRow(id=role_id, org_id=ORG_ID, name=name, permissions=[]))
        await session.commit()
    return role_id


# ===========================================================================
# IAM-300 — create / get / list
# ===========================================================================


class TestCreateGetList:
    @pytest.mark.anyio
    async def test_create_get_round_trip(self, sf):
        sa = await create_service_account(sf, org_id=ORG_ID, name="bot-1")
        assert sa.status == SERVICE_ACCOUNT_ACTIVE
        fetched = await get_service_account(sf, service_account_id=sa.id)
        assert fetched is not None
        assert fetched.id == sa.id
        assert fetched.org_id == ORG_ID
        assert fetched.name == "bot-1"

    @pytest.mark.anyio
    async def test_create_with_traceability_fields_persists(self, sf):
        review = datetime.now(UTC) + timedelta(days=90)
        sa = await create_service_account(
            sf,
            org_id=ORG_ID,
            name="ci-runner",
            description="runs CI jobs",
            owner_user_id="u-owner",
            purpose="ci",
            system="github-actions",
            environment="prod",
            expires_at=review,
        )
        fetched = await get_service_account(sf, service_account_id=sa.id)
        assert fetched is not None
        assert fetched.description == "runs CI jobs"
        assert fetched.owner_user_id == "u-owner"
        assert fetched.purpose == "ci"
        assert fetched.system == "github-actions"
        assert fetched.environment == "prod"
        # SQLite strips tzinfo on round-trip; compare the naive wall time.
        assert fetched.expires_at is not None
        assert fetched.expires_at.replace(tzinfo=None) == review.replace(tzinfo=None)

    @pytest.mark.anyio
    async def test_create_duplicate_org_name_raises(self, sf):
        await create_service_account(sf, org_id=ORG_ID, name="bot")
        with pytest.raises(IntegrityError):
            await create_service_account(sf, org_id=ORG_ID, name="bot")

    @pytest.mark.anyio
    async def test_create_same_name_in_different_orgs_allowed(self, sf):
        a = await create_service_account(sf, org_id=ORG_ID, name="bot")
        b = await create_service_account(sf, org_id=OTHER_ORG_ID, name="bot")
        assert a.id != b.id
        assert a.org_id != b.org_id

    @pytest.mark.anyio
    async def test_get_missing_returns_none(self, sf):
        assert await get_service_account(sf, service_account_id="nope") is None

    @pytest.mark.anyio
    async def test_list_scoped_to_org(self, sf):
        await create_service_account(sf, org_id=ORG_ID, name="bot-a")
        await create_service_account(sf, org_id=ORG_ID, name="bot-b")
        await create_service_account(sf, org_id=OTHER_ORG_ID, name="bot-c")
        rows = await list_service_accounts(sf, org_id=ORG_ID)
        assert {r.name for r in rows} == {"bot-a", "bot-b"}


# ===========================================================================
# IAM-301 — update / set_status
# ===========================================================================


class TestUpdateStatus:
    @pytest.mark.anyio
    async def test_update_each_field(self, sf):
        sa = await create_service_account(sf, org_id=ORG_ID, name="bot")
        updated = await update_service_account(
            sf,
            service_account_id=sa.id,
            description="new desc",
            owner_user_id="u-new",
            purpose="new purpose",
            system="new sys",
            environment="staging",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        assert updated.description == "new desc"
        assert updated.owner_user_id == "u-new"
        assert updated.purpose == "new purpose"
        assert updated.system == "new sys"
        assert updated.environment == "staging"

    @pytest.mark.anyio
    async def test_update_partial_only_changes_named_fields(self, sf):
        sa = await create_service_account(sf, org_id=ORG_ID, name="bot", purpose="keep")
        updated = await update_service_account(sf, service_account_id=sa.id, environment="prod")
        assert updated.environment == "prod"
        assert updated.purpose == "keep"

    @pytest.mark.anyio
    async def test_update_rejects_status_field(self, sf):
        sa = await create_service_account(sf, org_id=ORG_ID, name="bot")
        with pytest.raises(ValueError, match="non-updatable"):
            # ``status`` lives on _UPDATABLE_FIELDS deny-list; passing it as
            # a kwarg raises before any DB touch.
            await update_service_account(sf, service_account_id=sa.id, status="disabled")  # type: ignore[call-arg]

    @pytest.mark.anyio
    async def test_update_missing_raises_value_error(self, sf):
        with pytest.raises(ValueError):
            await update_service_account(sf, service_account_id="nope", description="x")

    @pytest.mark.anyio
    async def test_set_status_active_disabled_round_trip(self, sf):
        sa = await create_service_account(sf, org_id=ORG_ID, name="bot")
        disabled = await set_service_account_status(sf, service_account_id=sa.id, status=SERVICE_ACCOUNT_DISABLED)
        assert disabled.status == SERVICE_ACCOUNT_DISABLED
        re_enabled = await set_service_account_status(sf, service_account_id=sa.id, status=SERVICE_ACCOUNT_ACTIVE)
        assert re_enabled.status == SERVICE_ACCOUNT_ACTIVE

    @pytest.mark.anyio
    async def test_set_status_rejects_unknown(self, sf):
        sa = await create_service_account(sf, org_id=ORG_ID, name="bot")
        with pytest.raises(ValueError, match="Unknown ServiceAccount status"):
            await set_service_account_status(sf, service_account_id=sa.id, status="deleted")

    @pytest.mark.anyio
    async def test_set_status_missing_raises_value_error(self, sf):
        with pytest.raises(ValueError):
            await set_service_account_status(sf, service_account_id="nope", status="disabled")


# ===========================================================================
# IAM-302 — delete (cascade + atomicity)
# ===========================================================================


class TestDelete:
    @pytest.mark.anyio
    async def test_delete_removes_row(self, sf):
        sa = await create_service_account(sf, org_id=ORG_ID, name="bot")
        await delete_service_account(sf, service_account_id=sa.id)
        assert await get_service_account(sf, service_account_id=sa.id) is None

    @pytest.mark.anyio
    async def test_delete_missing_is_noop(self, sf):
        # Idempotent — does not raise.
        await delete_service_account(sf, service_account_id="never-existed")

    @pytest.mark.anyio
    async def test_delete_clears_role_bindings_same_transaction(self, sf):
        """ADR §12: SA deletion MUST land with full cleanup atomically.

        ``role_bindings.principal_id`` has no FK (polymorphic, §5.2), so
        the repository helper DELETEs the binding rows in the same
        ``AsyncSession`` as the SA row. This test would fail if the
        binding cleanup were dropped, skipped, or moved out of the
        transaction.
        """
        role_id = await _seed_role(sf)
        sa = await create_service_account(sf, org_id=ORG_ID, name="bot")
        await create_role_binding(
            sf,
            org_id=ORG_ID,
            principal_type="service_account",
            principal_id=sa.id,
            role_id=role_id,
        )
        # Sanity: the binding exists.
        assert len(await list_role_bindings(sf, org_id=ORG_ID, principal_type="service_account", principal_id=sa.id)) == 1

        await delete_service_account(sf, service_account_id=sa.id)

        # Binding is gone even though it had no FK to enforce it.
        remaining = await list_role_bindings(sf, org_id=ORG_ID, principal_type="service_account", principal_id=sa.id)
        assert remaining == []


# ===========================================================================
# IAM-303 — role bindings (polymorphic helpers)
# ===========================================================================


class TestRoleBindings:
    @pytest.mark.anyio
    async def test_create_role_binding_rejects_unknown_principal_type(self, sf):
        # CHECK constraint allows only 'user' / 'service_account'.
        role_id = await _seed_role(sf)
        with pytest.raises(IntegrityError):
            await create_role_binding(
                sf,
                org_id=ORG_ID,
                principal_type="robot",
                principal_id="p-1",
                role_id=role_id,
            )

    @pytest.mark.anyio
    async def test_create_role_binding_unique_constraint(self, sf):
        role_id = await _seed_role(sf)
        await create_role_binding(
            sf,
            org_id=ORG_ID,
            principal_type="service_account",
            principal_id="sa-1",
            role_id=role_id,
        )
        with pytest.raises(IntegrityError):
            await create_role_binding(
                sf,
                org_id=ORG_ID,
                principal_type="service_account",
                principal_id="sa-1",
                role_id=role_id,
            )

    @pytest.mark.anyio
    async def test_list_role_bindings_scoped(self, sf):
        role_id = await _seed_role(sf)
        await create_role_binding(
            sf,
            org_id=ORG_ID,
            principal_type="service_account",
            principal_id="sa-1",
            role_id=role_id,
        )
        await create_role_binding(
            sf,
            org_id=ORG_ID,
            principal_type="user",
            principal_id="u-1",
            role_id=role_id,
        )
        sa_bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="service_account", principal_id="sa-1")
        assert len(sa_bindings) == 1
        assert sa_bindings[0].principal_type == "service_account"

    @pytest.mark.anyio
    async def test_delete_role_binding_org_scoped(self, sf):
        """delete_role_binding filters by org_id — a cross-Org id is a no-op."""
        role_id = await _seed_role(sf)
        # Seed one binding in ORG_ID, one in OTHER_ORG_ID.
        await create_role_binding(
            sf,
            org_id=ORG_ID,
            principal_type="service_account",
            principal_id="sa-1",
            role_id=role_id,
        )
        async with sf() as session:
            session.add(
                RoleBindingRow(
                    id="b-other",
                    org_id=OTHER_ORG_ID,
                    principal_type="service_account",
                    principal_id="sa-other",
                    role_id=role_id,
                )
            )
            await session.commit()

        # Deleting with org_id=ORG_ID + the OTHER_ORG_ID binding's id
        # affects zero rows: the org filter excludes the foreign binding.
        await delete_role_binding(sf, binding_id="b-other", org_id=ORG_ID)

        async with sf() as session:
            other = await session.get(RoleBindingRow, "b-other")
        assert other is not None, "cross-Org delete must not touch the foreign binding"

        # Deleting with the right org_id does remove the row.
        await delete_role_binding(sf, binding_id="b-other", org_id=OTHER_ORG_ID)
        async with sf() as session:
            assert await session.get(RoleBindingRow, "b-other") is None
