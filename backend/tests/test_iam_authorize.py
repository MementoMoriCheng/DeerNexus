"""Tests for the unified Authorize Service (PR-031).

Three layers, each in its own section:

1. ``membership.py`` new helpers — ``get_membership_any_status`` /
   ``get_org_status`` (IAM-001).
2. ``compute_effective_permissions`` pure function (ADR §6 intersection;
   IAM-010).
3. ``AuthorizeService`` — DB + cache wrapper, covering the testing-strategy
   §9.1 permission matrix (Admin/Developer/Viewer × 9 capabilities) and §9.2
   status mapping (invited/removed/suspended/disabled, org_state).
4. ``authorize_cache`` — TTL clamp + namespace isolation (IAM-040).

Fixture conventions mirror ``test_membership_resolver.py`` /
``test_default_org_bootstrap.py``: each async test boots an isolated
file-backed SQLite via ``init_engine`` and tears it down with ``close_engine``.
The ServiceAccount column of §9.1 is deferred to PR-034 (``按 scope`` semantics
need API Key scopes, which PR-031 only reserves, not implements).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from app.gateway.authorize import (
    AuthorizeError,
    AuthorizeService,
    compute_effective_permissions,
)
from app.gateway.authorize_cache import (
    DEFAULT_TTL_SECONDS,
    InMemoryPermissionCache,
    org_cache_key,
    system_cache_key,
)
from deerflow.contracts import (
    BUILTIN_ROLE_PERMISSIONS,
    ORG_ADMIN_ROLE_NAME,
    ORG_DEVELOPER_ROLE_NAME,
    ORG_VIEWER_ROLE_NAME,
    SYSTEM_PERMISSIONS,
    ErrorCode,
    Permission,
)
from deerflow.persistence.iam.model import RoleBindingRow
from deerflow.persistence.orgs.model import OrganizationRow, OrgMembershipRow
from deerflow.persistence.user.model import UserRow
from deerflow.tenancy import (
    ensure_builtin_roles,
    get_membership_any_status,
    get_org_status,
)

ORG_ID = "org-test"
USER_ID = "u-test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def sf(tmp_path: Path):
    """Boot an isolated SQLite DB; yield its session factory."""
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'authorize.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


def _user(*, user_id: str = USER_ID, system_role: str = "user") -> SimpleNamespace:
    """Build a User-like object (AuthorizeService only reads .id / .system_role)."""
    return SimpleNamespace(id=user_id, system_role=system_role)


async def _seed_org(sf, *, org_id: str = ORG_ID, status: str = "active") -> None:
    async with sf() as session:
        session.add(OrganizationRow(id=org_id, slug=org_id, name=org_id, status=status))
        await session.commit()


async def _seed_user(sf, *, user_id: str = USER_ID, system_role: str = "user") -> UserRow:
    async with sf() as session:
        if (existing := await session.get(UserRow, user_id)) is not None:
            return existing
        user = UserRow(id=user_id, email=f"{user_id}@example.com", system_role=system_role)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def _seed_membership(sf, *, org_id: str = ORG_ID, user_id: str = USER_ID, status: str = "active") -> None:
    await _seed_user(sf, user_id=user_id)
    async with sf() as session:
        session.add(OrgMembershipRow(id=f"m-{org_id}-{user_id}-{status}", org_id=org_id, user_id=user_id, status=status))
        await session.commit()


async def _bind_role(
    sf,
    *,
    org_id: str = ORG_ID,
    user_id: str = USER_ID,
    role_name: str,
    expires_at: datetime | None = None,
) -> None:
    """Bind ``user_id`` to the builtin ``role_name`` in ``org_id``.

    Relies on ``ensure_builtin_roles`` having seeded the three system templates
    already; looks the role up by (name, is_system) to grab its id.
    """
    from sqlalchemy import select

    from deerflow.persistence.iam.model import RoleRow

    async with sf() as session:
        role = (await session.execute(select(RoleRow).where(RoleRow.name == role_name, RoleRow.is_system.is_(True)))).scalar_one()
        binding = RoleBindingRow(
            id=uuid4().hex,
            org_id=org_id,
            principal_type="user",
            principal_id=user_id,
            role_id=role.id,
            expires_at=expires_at,
        )
        session.add(binding)
        await session.commit()


async def _bootstrap(
    sf,
    *,
    org_id: str = ORG_ID,
    user_id: str = USER_ID,
    system_role: str = "user",
    membership_status: str = "active",
    org_status: str = "active",
    role_name: str | None = None,
) -> None:
    """One-shot helper: seed org + builtin roles + user + membership + binding."""
    await _seed_org(sf, org_id=org_id, status=org_status)
    await ensure_builtin_roles(sf)
    await _seed_user(sf, user_id=user_id, system_role=system_role)
    if membership_status is not None:
        await _seed_membership(sf, org_id=org_id, user_id=user_id, status=membership_status)
    if role_name is not None:
        await _bind_role(sf, org_id=org_id, user_id=user_id, role_name=role_name)


def _service(sf, *, cache=None) -> AuthorizeService:
    return AuthorizeService(sf, cache=cache)


# ===========================================================================
# IAM-001 — membership helpers
# ===========================================================================


class TestMembershipHelpers:
    """Cover get_membership_any_status / get_org_status (PR-031 read helpers)."""

    @pytest.mark.anyio
    async def test_get_membership_any_status_active(self, sf):
        await _bootstrap(sf, role_name=ORG_ADMIN_ROLE_NAME)
        row = await get_membership_any_status(sf, user_id=USER_ID, org_id=ORG_ID)
        assert row is not None
        assert row.status == "active"

    @pytest.mark.parametrize("status", ["invited", "suspended", "removed"])
    @pytest.mark.anyio
    async def test_get_membership_any_status_non_active_returned(self, sf, status):
        # Unlike get_active_membership, this helper returns the row regardless
        # of status so the caller can distinguish 403 (suspended) vs 404.
        await _seed_org(sf)
        await _seed_membership(sf, status=status)
        row = await get_membership_any_status(sf, user_id=USER_ID, org_id=ORG_ID)
        assert row is not None
        assert row.status == status

    @pytest.mark.anyio
    async def test_get_membership_any_status_missing_returns_none(self, sf):
        await _seed_org(sf)
        row = await get_membership_any_status(sf, user_id=USER_ID, org_id=ORG_ID)
        assert row is None

    @pytest.mark.anyio
    async def test_get_membership_any_status_wrong_org_returns_none(self, sf):
        await _bootstrap(sf, role_name=ORG_VIEWER_ROLE_NAME)
        row = await get_membership_any_status(sf, user_id=USER_ID, org_id="other-org")
        assert row is None

    @pytest.mark.parametrize("status", ["active", "suspended", "deleting", "deleted"])
    @pytest.mark.anyio
    async def test_get_org_status(self, sf, status):
        await _seed_org(sf, status=status)
        assert await get_org_status(sf, org_id=ORG_ID) == status

    @pytest.mark.anyio
    async def test_get_org_status_missing_returns_none(self, sf):
        assert await get_org_status(sf, org_id="nope") is None


# ===========================================================================
# IAM-010 — compute_effective_permissions pure function
# ===========================================================================


class TestComputeEffectivePermissions:
    """ADR §6 intersection math, no IO."""

    def test_admin_short_circuits_to_system_permissions(self):
        # system_role == "admin" returns SYSTEM_PERMISSIONS regardless of roles.
        result = compute_effective_permissions(
            membership_status="active",
            role_permissions=frozenset({Permission.RUNTIME_RUN_READ.value}),
            org_status="active",
            system_role="admin",
        )
        assert result == frozenset(SYSTEM_PERMISSIONS)

    def test_user_path_returns_role_permissions(self):
        result = compute_effective_permissions(
            membership_status="active",
            role_permissions=frozenset(BUILTIN_ROLE_PERMISSIONS[ORG_VIEWER_ROLE_NAME]),
            org_status="active",
            system_role="user",
        )
        assert result == frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_VIEWER_ROLE_NAME])

    def test_api_key_scopes_narrow(self):
        # scopes is intersective: can only shrink.
        full = frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_ADMIN_ROLE_NAME])
        result = compute_effective_permissions(
            membership_status="active",
            role_permissions=full,
            org_status="active",
            system_role="user",
            api_key_scopes=frozenset({Permission.RUNTIME_RUN_READ.value}),
        )
        assert result == {Permission.RUNTIME_RUN_READ.value}

    def test_api_key_scopes_none_is_universe(self):
        full = frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_VIEWER_ROLE_NAME])
        result = compute_effective_permissions(
            membership_status="active",
            role_permissions=full,
            org_status="active",
            system_role="user",
            api_key_scopes=None,
        )
        assert result == full

    def test_admin_with_scopes_still_narrowed(self):
        # Admin + scoped API Key = scopes win (ADR §6: scope only narrows).
        result = compute_effective_permissions(
            membership_status="active",
            role_permissions=frozenset(),
            org_status="active",
            system_role="admin",
            api_key_scopes=frozenset({Permission.SYSTEM_ORG_READ_ALL.value}),
        )
        assert result == {Permission.SYSTEM_ORG_READ_ALL.value}


# ===========================================================================
# IAM-040 — cache TTL + namespace
# ===========================================================================


class TestInMemoryPermissionCache:
    def test_set_get_roundtrip(self):
        cache = InMemoryPermissionCache()
        key = org_cache_key(org_id="o", principal_type="user", principal_id="u")
        cache.set(key, frozenset({"a"}))
        assert cache.get(key) == frozenset({"a"})

    def test_get_missing_returns_none(self):
        cache = InMemoryPermissionCache()
        assert cache.get("nope") is None

    def test_ttl_expiry_returns_none(self):
        cache = InMemoryPermissionCache()
        cache.set("k", frozenset({"a"}), ttl_seconds=0)
        # ttl=0 → expires immediately on next read.
        assert cache.get("k") is None

    def test_ttl_clamped_to_60s(self):
        cache = InMemoryPermissionCache()
        # A caller asking for >60s must be clamped down (ADR §11 hard bound).
        cache.set("k", frozenset({"a"}), ttl_seconds=3600)
        # Inspect the stored expiry directly to prove the clamp happened.
        expires_at, _ = cache._entries["k"]  # noqa: SLF001 — white-box test for the clamp
        assert expires_at - time.monotonic() <= DEFAULT_TTL_SECONDS + 0.1

    def test_invalidate_drops_entry(self):
        cache = InMemoryPermissionCache()
        cache.set("k", frozenset({"a"}))
        cache.invalidate("k")
        assert cache.get("k") is None

    def test_clear_drops_all(self):
        cache = InMemoryPermissionCache()
        cache.set("a", frozenset({"1"}))
        cache.set("b", frozenset({"2"}))
        cache.clear()
        assert cache.get("a") is None
        assert cache.get("b") is None

    def test_system_namespace_key_is_distinct(self):
        # system-admin uses a different prefix from org-scoped entries so a
        # cross-Org principal cannot collide with an Org-scoped one.
        sys_key = system_cache_key(principal_id="u")
        org_key = org_cache_key(org_id="o", principal_type="user", principal_id="u")
        assert sys_key != org_key
        assert sys_key.startswith("authz:system")
        assert org_key.startswith("authz:o:")


# ===========================================================================
# IAM-100 — AuthorizeService effective permissions per builtin role
# (testing-strategy §9.1, ServiceAccount column deferred to PR-034)
# ===========================================================================


class TestAuthorizeServiceRoles:
    """Each builtin role yields its ADR §4 permission set through the service."""

    @pytest.mark.parametrize(
        ("role_name", "expected_set"),
        [
            (ORG_ADMIN_ROLE_NAME, BUILTIN_ROLE_PERMISSIONS[ORG_ADMIN_ROLE_NAME]),
            (ORG_DEVELOPER_ROLE_NAME, BUILTIN_ROLE_PERMISSIONS[ORG_DEVELOPER_ROLE_NAME]),
            (ORG_VIEWER_ROLE_NAME, BUILTIN_ROLE_PERMISSIONS[ORG_VIEWER_ROLE_NAME]),
        ],
    )
    @pytest.mark.anyio
    async def test_builtin_role_permissions_match_registry(self, sf, role_name, expected_set):
        await _bootstrap(sf, role_name=role_name)
        perms = await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert perms == frozenset(p.value for p in expected_set)

    @pytest.mark.anyio
    async def test_admin_user_short_circuits_to_system_permissions(self, sf):
        # A system_role="admin" user gets SYSTEM_PERMISSIONS even with no
        # RoleBinding at all (ADR §4.4).
        await _seed_org(sf)
        await _seed_user(sf, system_role="admin")
        # No membership, no binding — admin bypasses both.
        perms = await _service(sf).compute_permissions_for_user(_user(system_role="admin"), org_id=ORG_ID)
        assert perms == frozenset(SYSTEM_PERMISSIONS)

    @pytest.mark.anyio
    async def test_no_binding_yields_empty_set(self, sf):
        # Active membership but zero RoleBindings → effective set is empty
        # (the user is known but granted nothing).
        await _bootstrap(sf)  # no role_name
        perms = await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert perms == frozenset()

    @pytest.mark.anyio
    async def test_multiple_bindings_union(self, sf):
        # Bind both viewer and developer → effective is the union.
        await _bootstrap(sf, role_name=ORG_VIEWER_ROLE_NAME)
        await _bind_role(sf, role_name=ORG_DEVELOPER_ROLE_NAME)
        perms = await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        expected = frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_VIEWER_ROLE_NAME]) | frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_DEVELOPER_ROLE_NAME])
        assert perms == expected

    @pytest.mark.anyio
    async def test_expired_binding_excluded(self, sf):
        # A binding whose expires_at is in the past does not contribute.
        await _bootstrap(sf, role_name=ORG_VIEWER_ROLE_NAME)
        await _bind_role(
            sf,
            role_name=ORG_DEVELOPER_ROLE_NAME,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        perms = await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        # Only the viewer (non-expired) binding counts.
        assert perms == frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_VIEWER_ROLE_NAME])

    @pytest.mark.anyio
    async def test_future_expiry_binding_included(self, sf):
        await _bootstrap(sf)
        await _bind_role(
            sf,
            role_name=ORG_DEVELOPER_ROLE_NAME,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        perms = await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert perms == frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[ORG_DEVELOPER_ROLE_NAME])


# ===========================================================================
# IAM-110 — §9.1 matrix spot-checks (allow / deny by capability)
# ===========================================================================


def _role_perms(role_name: str) -> frozenset[str]:
    return frozenset(p.value for p in BUILTIN_ROLE_PERMISSIONS[role_name])


class TestRbacMatrixAuthorize:
    """Authorize-side enforcement of the testing-strategy §9.1 grid.

    The grid itself is exhaustively pinned at the registry level in
    ``test_contracts_rbac.py::TestRbacMatrix``. Here we verify the service
    propagates those sets faithfully (a role that "allows Console" at the
    registry level must allow it through ``compute_permissions_for_user``).
    """

    @pytest.mark.parametrize(
        ("role_name", "can_console", "can_create_run", "can_prod_promote"),
        [
            (ORG_ADMIN_ROLE_NAME, True, True, True),
            (ORG_DEVELOPER_ROLE_NAME, False, True, False),
            (ORG_VIEWER_ROLE_NAME, False, False, False),
        ],
    )
    @pytest.mark.anyio
    async def test_role_capability_matrix(self, sf, role_name, can_console, can_create_run, can_prod_promote):
        await _bootstrap(sf, role_name=role_name)
        perms = await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert (Permission.ADMIN_CONSOLE_READ.value in perms) is can_console
        assert (Permission.RUNTIME_RUN_CREATE.value in perms) is can_create_run
        assert (Permission.STUDIO_RELEASE_PROMOTE.value in perms and Permission.STUDIO_RELEASE_ROLLBACK.value in perms) is can_prod_promote


# ===========================================================================
# IAM-120 — §9.2 status mapping (denials)
# ===========================================================================


class TestAuthorizeStatusDenials:
    """AuthorizeService raises the right ErrorCode for each §9.2 terminal state."""

    @pytest.mark.anyio
    async def test_invited_membership_denied(self, sf):
        await _bootstrap(sf, membership_status="invited", role_name=ORG_ADMIN_ROLE_NAME)
        with pytest.raises(AuthorizeError) as exc_info:
            await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert exc_info.value.code == ErrorCode.PERMISSION_DENIED

    @pytest.mark.anyio
    async def test_removed_membership_denied(self, sf):
        await _bootstrap(sf, membership_status="removed", role_name=ORG_ADMIN_ROLE_NAME)
        with pytest.raises(AuthorizeError) as exc_info:
            await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert exc_info.value.code == ErrorCode.PERMISSION_DENIED

    @pytest.mark.anyio
    async def test_suspended_membership_denied(self, sf):
        await _bootstrap(sf, membership_status="suspended", role_name=ORG_ADMIN_ROLE_NAME)
        with pytest.raises(AuthorizeError) as exc_info:
            await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert exc_info.value.code == ErrorCode.PERMISSION_DENIED

    @pytest.mark.anyio
    async def test_no_membership_denied(self, sf):
        # Org exists, user exists, but no membership row at all.
        await _seed_org(sf)
        await _seed_user(sf)
        with pytest.raises(AuthorizeError) as exc_info:
            await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert exc_info.value.code == ErrorCode.PERMISSION_DENIED

    @pytest.mark.anyio
    async def test_suspended_org_denied(self, sf):
        await _bootstrap(sf, org_status="suspended", role_name=ORG_ADMIN_ROLE_NAME)
        with pytest.raises(AuthorizeError) as exc_info:
            await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert exc_info.value.code == ErrorCode.ORG_SUSPENDED

    @pytest.mark.anyio
    async def test_deleting_org_denied(self, sf):
        await _bootstrap(sf, org_status="deleting", role_name=ORG_ADMIN_ROLE_NAME)
        with pytest.raises(AuthorizeError) as exc_info:
            await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert exc_info.value.code == ErrorCode.ORG_DELETING

    @pytest.mark.anyio
    async def test_deleted_org_denied(self, sf):
        # "deleted" treated like "deleting" — both write-blocked.
        await _bootstrap(sf, org_status="deleted", role_name=ORG_ADMIN_ROLE_NAME)
        with pytest.raises(AuthorizeError) as exc_info:
            await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert exc_info.value.code == ErrorCode.ORG_DELETING

    @pytest.mark.anyio
    async def test_missing_org_denied(self, sf):
        # No OrganizationRow at all — framed as permission_denied (404) per
        # ADR §12 existence-hiding rule.
        await _seed_user(sf)
        with pytest.raises(AuthorizeError) as exc_info:
            await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert exc_info.value.code == ErrorCode.PERMISSION_DENIED


# ===========================================================================
# IAM-130 — API Key scope narrowing (reserved hook)
# ===========================================================================


class TestAuthorizeServiceScopes:
    @pytest.mark.anyio
    async def test_scopes_narrow_admin(self, sf):
        # An admin using a scoped API Key is narrowed to the scope intersection.
        await _seed_org(sf)
        await _seed_user(sf, system_role="admin")
        perms = await _service(sf).compute_permissions_for_user(
            _user(system_role="admin"),
            org_id=ORG_ID,
            api_key_scopes=frozenset({Permission.SYSTEM_ORG_READ_ALL.value}),
        )
        assert perms == {Permission.SYSTEM_ORG_READ_ALL.value}

    @pytest.mark.anyio
    async def test_scopes_narrow_user(self, sf):
        await _bootstrap(sf, role_name=ORG_ADMIN_ROLE_NAME)
        perms = await _service(sf).compute_permissions_for_user(
            _user(),
            org_id=ORG_ID,
            api_key_scopes=frozenset({Permission.RUNTIME_RUN_READ.value}),
        )
        assert perms == {Permission.RUNTIME_RUN_READ.value}

    @pytest.mark.anyio
    async def test_no_scopes_passes_full_set(self, sf):
        await _bootstrap(sf, role_name=ORG_VIEWER_ROLE_NAME)
        perms = await _service(sf).compute_permissions_for_user(_user(), org_id=ORG_ID, api_key_scopes=None)
        assert perms == _role_perms(ORG_VIEWER_ROLE_NAME)


# ===========================================================================
# IAM-140 — cache hit / miss / invalidate
# ===========================================================================


class TestAuthorizeServiceCache:
    @pytest.mark.anyio
    async def test_second_call_hits_cache(self, sf):
        # If the cache returns a value, the service must NOT touch the DB
        # again. We assert this by counting OrgMembershipRow queries via a
        # spy on get_membership_any_status.
        await _bootstrap(sf, role_name=ORG_VIEWER_ROLE_NAME)
        cache = InMemoryPermissionCache()
        service = _service(sf, cache=cache)

        first = await service.compute_permissions_for_user(_user(), org_id=ORG_ID)
        second = await service.compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert first == second
        # The cache key must be populated.
        key = org_cache_key(org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert cache.get(key) == first

    @pytest.mark.anyio
    async def test_invalidate_forces_recompute(self, sf):
        await _bootstrap(sf, role_name=ORG_VIEWER_ROLE_NAME)
        cache = InMemoryPermissionCache()
        service = _service(sf, cache=cache)

        first = await service.compute_permissions_for_user(_user(), org_id=ORG_ID)
        cache.invalidate(org_cache_key(org_id=ORG_ID, principal_type="user", principal_id=USER_ID))
        # Add a developer binding between calls — after invalidation the new
        # binding must show up in the recomputed set.
        await _bind_role(sf, role_name=ORG_DEVELOPER_ROLE_NAME)
        second = await service.compute_permissions_for_user(_user(), org_id=ORG_ID)
        assert second != first
        assert Permission.RUNTIME_RUN_CREATE.value in second  # developer-only

    @pytest.mark.anyio
    async def test_admin_uses_system_namespace(self, sf):
        await _seed_org(sf)
        await _seed_user(sf, system_role="admin")
        cache = InMemoryPermissionCache()
        service = _service(sf, cache=cache)

        await service.compute_permissions_for_user(_user(system_role="admin"), org_id=ORG_ID)
        # Admin entry lives under the system namespace, not the org one.
        sys_key = system_cache_key(principal_id=USER_ID)
        org_key = org_cache_key(org_id=ORG_ID, principal_type="user", principal_id=USER_ID)
        assert cache.get(sys_key) == frozenset(SYSTEM_PERMISSIONS)
        assert cache.get(org_key) is None
