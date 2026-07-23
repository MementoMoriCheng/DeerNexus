"""DB CRUD tests for the OIDC group-mapping repository helpers (PR-036).

Covers :mod:`deerflow.persistence.iam.repository`'s PR-036 additions
(``create/get/list/update/delete_oidc_group_mapping`` +
``count_user_bindings_for_role``) against an isolated SQLite. The
repository is pure data access (no audit / cache / authz / policy), so
these tests exercise only the DB invariants — allowlist uniqueness, the
mode CHECK constraint, Org-scoped filtering, and the last-admin count
query semantics.

Fixture conventions mirror ``test_iam_service_account_repository.py``
and ``test_api_key_repository.py``: boot an isolated SQLite via
``init_engine``, yield ``get_session_factory()``, tear down with
``close_engine``.

IAM IDs: ``IAM-360`` series (repository layer; service layer uses
``IAM-361``, last-admin policy ``IAM-362``, router ``IAM-363``, E2E
``IAM-364``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.iam.model import RoleRow
from deerflow.persistence.iam.repository import (
    MAPPING_MODE_ADDITIVE,
    MAPPING_MODE_AUTHORITATIVE,
    count_user_bindings_for_role,
    create_oidc_group_mapping,
    create_role_binding,
    delete_oidc_group_mapping,
    get_oidc_group_mapping,
    list_oidc_group_mappings,
    update_oidc_group_mapping,
)

ORG_ID = "org-test"
OTHER_ORG_ID = "org-other"
ISSUER = "https://idp.example.com"
ROLE_ID = "r-admin"
ROLE_DEV_ID = "r-dev"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'oidc_mapping_repo.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


async def _seed_role(sf, *, role_id: str = ROLE_ID, name: str = "org:admin", org_id: str = ORG_ID) -> str:
    async with sf() as session:
        session.add(RoleRow(id=role_id, org_id=org_id, name=name, permissions=[]))
        await session.commit()
    return role_id


async def _create(
    sf,
    *,
    issuer: str = ISSUER,
    group_value: str = "admins",
    target_org_id: str = ORG_ID,
    target_role_id: str = ROLE_ID,
    mode: str = MAPPING_MODE_ADDITIVE,
):
    return await create_oidc_group_mapping(
        sf,
        issuer=issuer,
        group_claim="groups",
        group_value=group_value,
        target_org_id=target_org_id,
        target_role_id=target_role_id,
        mode=mode,
    )


# ===========================================================================
# IAM-360 — create / get / list
# ===========================================================================


class TestCreateGetList:
    @pytest.mark.anyio
    async def test_create_defaults_to_additive_mode(self, sf):
        await _seed_role(sf)
        row = await _create(sf)
        assert row.mode == MAPPING_MODE_ADDITIVE
        assert row.issuer == ISSUER
        assert row.group_claim == "groups"
        assert row.group_value == "admins"
        assert row.created_by is None

    @pytest.mark.anyio
    async def test_create_explicit_authoritative_mode_stored(self, sf):
        await _seed_role(sf)
        row = await _create(sf, mode=MAPPING_MODE_AUTHORITATIVE)
        assert row.mode == MAPPING_MODE_AUTHORITATIVE

    @pytest.mark.anyio
    async def test_get_returns_row_by_id(self, sf):
        await _seed_role(sf)
        row = await _create(sf)
        fetched = await get_oidc_group_mapping(sf, mapping_id=row.id)
        assert fetched is not None
        assert fetched.id == row.id

    @pytest.mark.anyio
    async def test_get_missing_returns_none(self, sf):
        assert await get_oidc_group_mapping(sf, mapping_id="nope") is None

    @pytest.mark.anyio
    async def test_list_scoped_by_org_id(self, sf):
        await _seed_role(sf)
        await _create(sf, group_value="g1", target_org_id=ORG_ID)
        await _create(sf, group_value="g2", target_org_id=ORG_ID)
        await _create(sf, group_value="g3", target_org_id=OTHER_ORG_ID)
        rows = await list_oidc_group_mappings(sf, org_id=ORG_ID)
        assert {r.group_value for r in rows} == {"g1", "g2"}

    @pytest.mark.anyio
    async def test_list_scoped_by_issuer(self, sf):
        await _seed_role(sf)
        await _create(sf, issuer=ISSUER, group_value="g1")
        await _create(sf, issuer="https://other.idp", group_value="g2")
        rows = await list_oidc_group_mappings(sf, issuer=ISSUER)
        assert {r.group_value for r in rows} == {"g1"}

    @pytest.mark.anyio
    async def test_list_without_filter_returns_all(self, sf):
        await _seed_role(sf)
        await _create(sf, group_value="g1", target_org_id=ORG_ID)
        await _create(sf, group_value="g2", target_org_id=OTHER_ORG_ID)
        rows = await list_oidc_group_mappings(sf)
        assert len(rows) == 2


# ===========================================================================
# IAM-360 — uniqueness + CHECK constraints
# ===========================================================================


class TestConstraints:
    @pytest.mark.anyio
    async def test_duplicate_allowlist_entry_raises_integrity(self, sf):
        await _seed_role(sf)
        await _create(sf, group_value="admins")
        with pytest.raises(IntegrityError):
            await _create(sf, group_value="admins")

    @pytest.mark.anyio
    async def test_same_group_different_role_allowed(self, sf):
        """ADR §10 rule 4 (union): two rules may target the same group + role-different roles."""
        await _seed_role(sf, role_id=ROLE_ID, name="org:admin")
        await _seed_role(sf, role_id=ROLE_DEV_ID, name="org:developer")
        await _create(sf, group_value="admins", target_role_id=ROLE_ID)
        await _create(sf, group_value="admins", target_role_id=ROLE_DEV_ID)
        rows = await list_oidc_group_mappings(sf, org_id=ORG_ID)
        assert {r.target_role_id for r in rows} == {ROLE_ID, ROLE_DEV_ID}

    @pytest.mark.anyio
    async def test_same_group_different_org_allowed(self, sf):
        await _seed_role(sf, role_id=ROLE_ID, org_id=ORG_ID)
        await _seed_role(sf, role_id="r-other", org_id=OTHER_ORG_ID, name="org:admin")
        await _create(sf, group_value="admins", target_org_id=ORG_ID, target_role_id=ROLE_ID)
        await _create(sf, group_value="admins", target_org_id=OTHER_ORG_ID, target_role_id="r-other")
        rows = await list_oidc_group_mappings(sf)
        assert len(rows) == 2

    @pytest.mark.anyio
    async def test_invalid_mode_raises_integrity(self, sf):
        await _seed_role(sf)
        # Bypass the typed helper to exercise the CHECK directly.
        from deerflow.persistence.iam.model import OidcGroupMappingRow

        async with sf() as session:
            session.add(
                OidcGroupMappingRow(
                    id="x",
                    issuer=ISSUER,
                    group_claim="groups",
                    group_value="g",
                    target_org_id=ORG_ID,
                    target_role_id=ROLE_ID,
                    mode="bogus",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()


# ===========================================================================
# IAM-360 — update / delete
# ===========================================================================


class TestUpdateDelete:
    @pytest.mark.anyio
    async def test_update_patchable_fields(self, sf):
        await _seed_role(sf, role_id=ROLE_DEV_ID, name="org:developer")
        row = await _create(sf, group_value="admins", target_role_id=ROLE_ID)
        updated = await update_oidc_group_mapping(
            sf,
            mapping_id=row.id,
            group_value="ops",
            target_role_id=ROLE_DEV_ID,
            description="changed",
        )
        assert updated.group_value == "ops"
        assert updated.target_role_id == ROLE_DEV_ID
        assert updated.description == "changed"

    @pytest.mark.anyio
    async def test_update_rejects_immutable_target_org(self, sf):
        await _seed_role(sf)
        row = await _create(sf)
        with pytest.raises(ValueError):
            await update_oidc_group_mapping(sf, mapping_id=row.id, target_org_id=OTHER_ORG_ID)

    @pytest.mark.anyio
    async def test_update_rejects_issuer(self, sf):
        await _seed_role(sf)
        row = await _create(sf)
        with pytest.raises(ValueError):
            await update_oidc_group_mapping(sf, mapping_id=row.id, issuer="https://other")

    @pytest.mark.anyio
    async def test_update_missing_row_raises_valueerror(self, sf):
        with pytest.raises(ValueError):
            await update_oidc_group_mapping(sf, mapping_id="nope", group_value="x")

    @pytest.mark.anyio
    async def test_delete_scoped_by_org(self, sf):
        await _seed_role(sf)
        row = await _create(sf)
        await delete_oidc_group_mapping(sf, mapping_id=row.id, org_id=ORG_ID)
        assert await get_oidc_group_mapping(sf, mapping_id=row.id) is None

    @pytest.mark.anyio
    async def test_delete_wrong_org_is_noop(self, sf):
        """Cross-Org delete touches zero rows (existence-hiding, ADR §8)."""
        await _seed_role(sf)
        row = await _create(sf, target_org_id=ORG_ID)
        await delete_oidc_group_mapping(sf, mapping_id=row.id, org_id=OTHER_ORG_ID)
        assert await get_oidc_group_mapping(sf, mapping_id=row.id) is not None


# ===========================================================================
# IAM-360 — count_user_bindings_for_role (last-admin guard read)
# ===========================================================================


class TestCountUserBindingsForRole:
    @pytest.mark.anyio
    async def test_zero_when_no_bindings(self, sf):
        await _seed_role(sf)
        n = await count_user_bindings_for_role(sf, org_id=ORG_ID, role_id=ROLE_ID)
        assert n == 0

    @pytest.mark.anyio
    async def test_counts_user_bindings_excluding_service_accounts(self, sf):
        await _seed_role(sf)
        await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id="u-1", role_id=ROLE_ID)
        # A service_account binding must NOT count as a human admin (ADR §7).
        await create_role_binding(sf, org_id=ORG_ID, principal_type="service_account", principal_id="sa-1", role_id=ROLE_ID)
        n = await count_user_bindings_for_role(sf, org_id=ORG_ID, role_id=ROLE_ID)
        assert n == 1

    @pytest.mark.anyio
    async def test_exclude_principal_id(self, sf):
        await _seed_role(sf)
        await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id="u-1", role_id=ROLE_ID)
        await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id="u-2", role_id=ROLE_ID)
        n = await count_user_bindings_for_role(sf, org_id=ORG_ID, role_id=ROLE_ID, exclude_principal_id="u-1")
        assert n == 1

    @pytest.mark.anyio
    async def test_expired_binding_excluded(self, sf):
        """An expired binding does not count — last-admin uses live admins only."""
        await _seed_role(sf)
        past = datetime.now(UTC) - timedelta(hours=1)
        await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id="u-1", role_id=ROLE_ID, expires_at=past)
        n = await count_user_bindings_for_role(sf, org_id=ORG_ID, role_id=ROLE_ID)
        assert n == 0

    @pytest.mark.anyio
    async def test_exclude_principal_makes_remaining_zero(self, sf):
        """The last-admin scenario: the sole admin is the one under removal."""
        await _seed_role(sf)
        await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id="u-sole", role_id=ROLE_ID)
        n = await count_user_bindings_for_role(sf, org_id=ORG_ID, role_id=ROLE_ID, exclude_principal_id="u-sole")
        assert n == 0
