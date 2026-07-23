"""Service-layer tests for the OIDC group-mapping engine (PR-036, ADR-0003 §10).

Exercises :func:`deerflow.tenancy.oidc_group_mapping.apply_group_mapping`
end-to-end against an isolated SQLite, covering every ADR §10 additive
rule and the dry-run / authoritative / multi-membership branches. The
engine is IdP-agnostic (it takes ``(issuer, groups)``), so these tests
inject mock IdP claims directly — this IS the PR-036 IdP E2E surface
(the real OIDC code-flow / JWKS transport is a separate PR).

IAM IDs: ``IAM-361`` series (repository is ``IAM-360``, last-admin is
``IAM-362``, router ``IAM-363``, full authorize() integration ``IAM-364``).

ADR §10 additive rules under test:
  1. allowlist only        — unmatched group ignored
  2. no auto-create roles  — engine references existing roles
  3. no system perms       — target role with system:* skipped
  4. union                 — multiple groups each materialize their binding
  5. audit                 — emit_tenant_event on every disposition
  6. no auto-delete manual — additive only ensures; never removes
  7. authoritative gated   — authoritative rows logged + skipped
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.iam.model import RoleRow
from deerflow.persistence.iam.repository import (
    MAPPING_MODE_ADDITIVE,
    MAPPING_MODE_AUTHORITATIVE,
    create_oidc_group_mapping,
    create_role_binding,
    list_role_bindings,
)
from deerflow.persistence.orgs.model import ExternalIdentityRow, OrganizationRow, OrgMembershipRow
from deerflow.persistence.user.model import UserRow
from deerflow.tenancy.oidc_group_mapping import (
    GROUP_MAPPING_PROVENANCE_PREFIX,
    MappingResult,
    apply_group_mapping,
)

ORG_ID = "org-test"
OTHER_ORG_ID = "org-other"
ISSUER = "https://idp.example.com"
USER_ID = "u-1"
SUBJECT = "idp-sub-1"
ADMIN_ROLE_ID = "r-admin"
DEV_ROLE_ID = "r-dev"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'oidc_mapping_service.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


async def _seed_world(sf, *, user_id: str = USER_ID, org_id: str = ORG_ID) -> None:
    """Seed org + user + active membership (the engine's prerequisites).

    Parents (org, user) are committed in their own session BEFORE the
    membership row — SQLite FK-at-commit hygiene (see
    ``test_tenant_schema.py``): a single-session batch can flush the
    membership before its parents under some unit-of-work orderings.
    """
    async with sf() as session:
        session.add(OrganizationRow(id=org_id, slug=org_id, name=org_id, status="active"))
        session.add(UserRow(id=user_id, email=f"{user_id}@example.com", system_role="user"))
        await session.commit()
    async with sf() as session:
        session.add(OrgMembershipRow(id=f"m-{org_id}-{user_id}", org_id=org_id, user_id=user_id, status="active"))
        await session.commit()


async def _seed_role(
    sf,
    *,
    role_id: str = ADMIN_ROLE_ID,
    name: str = "org:admin",
    org_id: str = ORG_ID,
    permissions: list[str] | None = None,
    is_system: bool = False,
) -> str:
    async with sf() as session:
        session.add(
            RoleRow(
                id=role_id,
                org_id=org_id if not is_system else None,
                name=name,
                permissions=permissions or [],
                is_system=is_system,
            )
        )
        await session.commit()
    return role_id


async def _add_rule(
    sf,
    *,
    issuer: str = ISSUER,
    group_value: str = "admins",
    target_org_id: str = ORG_ID,
    target_role_id: str = ADMIN_ROLE_ID,
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
# IAM-361a — additive apply materializes a binding (rule 2: no auto-create)
# ===========================================================================


class TestAdditiveApply:
    @pytest.mark.anyio
    async def test_apply_creates_user_role_binding(self, sf):
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        result = await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)

        assert len(result.applied) == 1
        assert result.applied[0].group_value == "admins"
        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert len(bindings) == 1
        assert bindings[0].role_id == ADMIN_ROLE_ID

    @pytest.mark.anyio
    async def test_apply_provenance_sentinel_stamped(self, sf):
        """Rule 6 provenance: the binding's created_by records the mapping source."""
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)

        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert bindings[0].created_by == f"{GROUP_MAPPING_PROVENANCE_PREFIX}:{ISSUER}:admins"

    @pytest.mark.anyio
    async def test_apply_is_idempotent(self, sf):
        """Re-applying the same groups does not create a duplicate binding (unique constraint)."""
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)
        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)

        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert len(bindings) == 1

    @pytest.mark.anyio
    async def test_apply_preserves_manual_binding_created_by(self, sf):
        """A pre-existing manual binding is NOT overwritten on re-apply."""
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")
        # A human admin pre-bound the role manually.
        await create_role_binding(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID, role_id=ADMIN_ROLE_ID, created_by="u-operator")

        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)

        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert len(bindings) == 1
        assert bindings[0].created_by == "u-operator"  # manual attribution retained


# ===========================================================================
# IAM-361b — dry-run (writes nothing; records planned)
# ===========================================================================


class TestDryRun:
    @pytest.mark.anyio
    async def test_dry_run_writes_no_binding(self, sf):
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        result = await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT, dry_run=True)

        assert result.dry_run is True
        assert len(result.planned) == 1
        assert len(result.applied) == 0
        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert bindings == []

    @pytest.mark.anyio
    async def test_dry_run_upserts_no_external_identity(self, sf):
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT, dry_run=True)

        from sqlalchemy import select

        async with sf() as session:
            ext = (await session.execute(select(ExternalIdentityRow))).scalars().all()
        assert ext == []


# ===========================================================================
# IAM-361c — allowlist filter (rule 1: unmatched group ignored)
# ===========================================================================


class TestAllowlistFilter:
    @pytest.mark.anyio
    async def test_unmatched_group_ignored(self, sf):
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        result = await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["viewers", "unknown"], subject=SUBJECT)

        assert result.applied == []
        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert bindings == []

    @pytest.mark.anyio
    async def test_wrong_issuer_ignored(self, sf):
        """Rule 1: only allowlisted issuer maps; a foreign issuer hits nothing."""
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, issuer=ISSUER, group_value="admins")

        result = await apply_group_mapping(sf, user_id=USER_ID, issuer="https://evil.idp", groups=["admins"], subject=SUBJECT)
        assert result.applied == []


# ===========================================================================
# IAM-361d — no system permissions target (rule 3)
# ===========================================================================


class TestSystemPermissionTarget:
    @pytest.mark.anyio
    async def test_system_role_target_skipped(self, sf):
        """Rule 3: a target role carrying a system: permission is skipped."""
        await _seed_world(sf)
        # A system-template role with a system: permission (registry would
        # forbid this on an Org role, but defence-in-depth must still skip it).
        await _seed_role(
            sf,
            role_id="r-sys",
            name="system:admin",
            is_system=True,
            permissions=["system:org:operate_all"],
        )
        await _add_rule(sf, group_value="admins", target_role_id="r-sys")

        result = await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)

        assert result.applied == []
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == "target_role_missing_or_system"

    @pytest.mark.anyio
    async def test_missing_role_target_skipped(self, sf):
        await _seed_world(sf)
        await _add_rule(sf, group_value="admins", target_role_id="does-not-exist")

        result = await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)

        assert result.applied == []
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == "target_role_missing_or_system"


# ===========================================================================
# IAM-361e — union (rule 4: multiple groups each materialize a binding)
# ===========================================================================


class TestUnion:
    @pytest.mark.anyio
    async def test_two_groups_two_bindings(self, sf):
        await _seed_world(sf)
        await _seed_role(sf, role_id=ADMIN_ROLE_ID, name="org:admin")
        await _seed_role(sf, role_id=DEV_ROLE_ID, name="org:developer")
        await _add_rule(sf, group_value="admins", target_role_id=ADMIN_ROLE_ID)
        await _add_rule(sf, group_value="devs", target_role_id=DEV_ROLE_ID)

        result = await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins", "devs"], subject=SUBJECT)

        assert len(result.applied) == 2
        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert {b.role_id for b in bindings} == {ADMIN_ROLE_ID, DEV_ROLE_ID}


# ===========================================================================
# IAM-361f — authoritative mode gated (rule 7)
# ===========================================================================


class TestAuthoritativeGated:
    @pytest.mark.anyio
    async def test_authoritative_rule_skipped_not_enacted(self, sf):
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins", mode=MAPPING_MODE_AUTHORITATIVE)

        result = await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)

        assert result.applied == []
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == "authoritative_mode_not_enabled"
        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert bindings == []


# ===========================================================================
# IAM-361g — membership prerequisites
# ===========================================================================


class TestMembershipPrerequisites:
    @pytest.mark.anyio
    async def test_no_active_membership_returns_empty(self, sf):
        """User with no membership: engine logs + returns empty (no bind to a guessed org)."""
        async with sf() as session:
            session.add(OrganizationRow(id=ORG_ID, slug=ORG_ID, name=ORG_ID, status="active"))
            session.add(UserRow(id=USER_ID, email="u@example.com", system_role="user"))
            await session.commit()
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        result = await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)

        assert result.applied == [] and result.planned == []
        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert bindings == []

    @pytest.mark.anyio
    async def test_target_org_must_match_user_membership(self, sf):
        """Multi-membership deferral: a rule targeting another org is skipped."""
        await _seed_world(sf)
        # The rule targets OTHER_ORG_ID but the user is a member of ORG_ID.
        async with sf() as session:
            session.add(OrganizationRow(id=OTHER_ORG_ID, slug=OTHER_ORG_ID, name=OTHER_ORG_ID, status="active"))
            await session.commit()
        await _seed_role(sf, role_id="r-other", org_id=OTHER_ORG_ID, name="org:admin")
        await _add_rule(sf, group_value="admins", target_org_id=OTHER_ORG_ID, target_role_id="r-other")

        result = await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)

        assert result.applied == []
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == "target_org_not_user_membership"


# ===========================================================================
# IAM-361h — audit emission (rule 5)
# ===========================================================================


class TestAuditEmission:
    @pytest.mark.anyio
    async def test_apply_emits_applied_event(self, sf):
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        with patch("deerflow.tenancy.oidc_group_mapping.emit_tenant_event") as mock_emit:
            await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)

        event_types = [c.args[0] for c in mock_emit.call_args_list]
        assert "oidc_group_mapping_applied" in event_types

    @pytest.mark.anyio
    async def test_dry_run_emits_dry_run_event_not_applied(self, sf):
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        with patch("deerflow.tenancy.oidc_group_mapping.emit_tenant_event") as mock_emit:
            await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT, dry_run=True)

        event_types = [c.args[0] for c in mock_emit.call_args_list]
        assert "oidc_group_mapping_dry_run" in event_types
        assert "oidc_group_mapping_applied" not in event_types


# ===========================================================================
# IAM-361i — external identity upsert (§4.4 table brought to life)
# ===========================================================================


class TestExternalIdentityUpsert:
    @pytest.mark.anyio
    async def test_apply_upserts_external_identity_snapshot(self, sf):
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins", "viewers"], subject=SUBJECT)

        from sqlalchemy import select

        async with sf() as session:
            ext = (await session.execute(select(ExternalIdentityRow))).scalar_one()
        assert ext.issuer == ISSUER
        assert ext.subject == SUBJECT
        assert ext.user_id == USER_ID
        assert ext.claims_snapshot == {"groups": ["admins", "viewers"]}

    @pytest.mark.anyio
    async def test_re_apply_overwrites_snapshot(self, sf):
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)
        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins", "ops"], subject=SUBJECT)

        from sqlalchemy import select

        async with sf() as session:
            rows = (await session.execute(select(ExternalIdentityRow))).scalars().all()
        assert len(rows) == 1  # upsert, not insert
        assert rows[0].claims_snapshot == {"groups": ["admins", "ops"]}

    @pytest.mark.anyio
    async def test_apply_without_subject_skips_upsert(self, sf):
        """Subject unavailable → binding still lands; federated link skipped (observability-only)."""
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=None)

        from sqlalchemy import select

        async with sf() as session:
            rows = (await session.execute(select(ExternalIdentityRow))).scalars().all()
        assert rows == []
        # But the binding did land.
        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert len(bindings) == 1


# ===========================================================================
# IAM-361j — additive never removes (rule 6)
# ===========================================================================


class TestAdditiveNeverRemoves:
    @pytest.mark.anyio
    async def test_apply_does_not_remove_bindings_on_missing_group(self, sf):
        """A group dropped from the IdP claim does not auto-delete its binding (additive)."""
        await _seed_world(sf)
        await _seed_role(sf)
        await _add_rule(sf, group_value="admins")

        # First apply binds admins.
        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["admins"], subject=SUBJECT)
        # Second apply: the group is GONE from the claim. Additive must not remove.
        await apply_group_mapping(sf, user_id=USER_ID, issuer=ISSUER, groups=["viewers"], subject=SUBJECT)

        bindings = await list_role_bindings(sf, org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert len(bindings) == 1  # the admins binding survives


# ensure MappingResult is constructible (static contract check)
def test_mapping_result_is_dataclass():
    r = MappingResult(user_id=USER_ID, issuer=ISSUER, dry_run=True)
    assert r.planned == [] and r.applied == [] and r.skipped == []
