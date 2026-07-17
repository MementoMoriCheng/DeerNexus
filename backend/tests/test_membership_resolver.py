"""Tests for membership-based org resolution (PR-025C+).

Two layers:

1. ``get_active_membership`` helper (``deerflow.tenancy.membership``) — the
   single-membership-strict read (0 → None, 1 → row, >1 → MultiMembershipError).
2. ``resolve_tenant_context`` (``app.gateway.tenant``) — the phase gate:
   ``disabled`` fast path (single-Org, no DB) vs ``validation``/``active``
   membership path (fail-closed on no/multi membership, sf=None).

Follows the fixture conventions of ``test_default_org_bootstrap.py`` /
``test_validation_org_bootstrap.py``: isolated file-backed SQLite via
``init_engine`` / ``close_engine``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.orgs.model import OrganizationRow, OrgMembershipRow
from deerflow.persistence.user.model import UserRow
from deerflow.tenancy import MultiMembershipError, get_active_membership


@pytest.fixture
async def sf(tmp_path: Path):
    """Boot an isolated SQLite DB; yield its session factory."""
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'membership.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


async def _seed_org(sf, *, org_id: str) -> None:
    async with sf() as session:
        session.add(OrganizationRow(id=org_id, slug=org_id, name=org_id, status="active"))
        await session.commit()


async def _seed_user(sf, *, user_id: str) -> None:
    """Insert the parent user row OrgMembershipRow.user_id FKs reference.

    Idempotent: a repeat seed for the same user_id no-ops (so callers that
    seed a membership per-org for one user don't trip the email UNIQUE).
    """
    async with sf() as session:
        if await session.get(UserRow, user_id) is not None:
            return
        session.add(UserRow(id=user_id, email=f"{user_id}@example.com", system_role="user"))
        await session.commit()


async def _seed_membership(sf, *, org_id: str, user_id: str, status: str = "active") -> None:
    # Ensure the FK parent user exists (idempotent) before the membership row.
    await _seed_user(sf, user_id=user_id)
    async with sf() as session:
        session.add(OrgMembershipRow(id=f"m-{org_id}-{user_id}-{status}", org_id=org_id, user_id=user_id, status=status))
        await session.commit()


def _fake_user(*, user_id: str = "u-1") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, email="u@example.com")


def _set_phase(monkeypatch, phase: str) -> None:
    monkeypatch.setattr("app.gateway.tenant.current_multi_org_phase", lambda: phase, raising=False)


# ===========================================================================
# get_active_membership — single-membership-strict
# ===========================================================================


class TestGetActiveMembership:
    @pytest.mark.anyio
    async def test_zero_active_returns_none(self, sf):
        await _seed_org(sf, org_id="o-1")
        result = await get_active_membership(sf, user_id="u-none")
        assert result is None

    @pytest.mark.anyio
    async def test_one_active_returns_row(self, sf):
        await _seed_org(sf, org_id="o-1")
        await _seed_membership(sf, org_id="o-1", user_id="u-1")
        result = await get_active_membership(sf, user_id="u-1")
        assert result is not None
        assert result.org_id == "o-1"
        assert result.status == "active"

    @pytest.mark.anyio
    async def test_multiple_active_raises(self, sf):
        await _seed_org(sf, org_id="o-1")
        await _seed_org(sf, org_id="o-2")
        await _seed_membership(sf, org_id="o-1", user_id="u-1")
        await _seed_membership(sf, org_id="o-2", user_id="u-1")
        with pytest.raises(MultiMembershipError) as exc_info:
            await get_active_membership(sf, user_id="u-1")
        assert exc_info.value.count == 2
        assert exc_info.value.user_id == "u-1"

    @pytest.mark.anyio
    async def test_non_active_status_excluded(self, sf):
        # invited/suspended/removed memberships must not bind a context (§4.5).
        # Each non-active status seeded in a distinct org so the
        # uq_org_memberships_org_user UNIQUE(org_id, user_id) is not violated.
        for i, status in enumerate(("invited", "suspended", "removed"), start=1):
            org_id = f"o-{i}"
            await _seed_org(sf, org_id=org_id)
            await _seed_membership(sf, org_id=org_id, user_id="u-1", status=status)
        result = await get_active_membership(sf, user_id="u-1")
        assert result is None


# ===========================================================================
# resolve_tenant_context — phase gate
# ===========================================================================


class TestResolveTenantContextPhaseGate:
    @pytest.mark.anyio
    async def test_disabled_uses_default_org_no_db(self, sf, monkeypatch):
        # disabled phase: org comes from config, no membership lookup.
        _set_phase(monkeypatch, "disabled")
        # Point get_session_factory at the live factory; disabled path must NOT
        # call it, so even seeding no memberships is fine.
        monkeypatch.setattr(
            "deerflow.persistence.engine.get_session_factory",
            lambda: sf,
        )
        from app.gateway.tenant import resolve_tenant_context

        tenant = await resolve_tenant_context(_fake_user(), "session", "req-1", SimpleNamespace(headers={}))
        # default_org_id is "default" (DEFAULT_BOOTSTRAP_ORG_ID).
        assert tenant.org_id == "default"

    @pytest.mark.anyio
    async def test_validation_resolves_org_from_membership(self, sf, monkeypatch):
        await _seed_org(sf, org_id="o-1")
        await _seed_membership(sf, org_id="o-1", user_id="u-1")
        _set_phase(monkeypatch, "validation")
        monkeypatch.setattr(
            "deerflow.persistence.engine.get_session_factory",
            lambda: sf,
        )
        from app.gateway.tenant import resolve_tenant_context

        tenant = await resolve_tenant_context(_fake_user(user_id="u-1"), "session", "req-1", SimpleNamespace(headers={}))
        assert tenant.org_id == "o-1"

    @pytest.mark.anyio
    async def test_active_resolves_org_from_membership(self, sf, monkeypatch):
        await _seed_org(sf, org_id="o-1")
        await _seed_membership(sf, org_id="o-1", user_id="u-1")
        _set_phase(monkeypatch, "active")
        monkeypatch.setattr(
            "deerflow.persistence.engine.get_session_factory",
            lambda: sf,
        )
        from app.gateway.tenant import resolve_tenant_context

        tenant = await resolve_tenant_context(_fake_user(user_id="u-1"), "session", "req-1", SimpleNamespace(headers={}))
        assert tenant.org_id == "o-1"


# ===========================================================================
# Fail-closed paths (TEN-008: never synthesize a default org)
# ===========================================================================


class TestResolveFailClosed:
    @pytest.mark.anyio
    async def test_no_membership_raises(self, sf, monkeypatch):
        await _seed_org(sf, org_id="o-1")  # org exists but user has no membership
        _set_phase(monkeypatch, "validation")
        monkeypatch.setattr(
            "deerflow.persistence.engine.get_session_factory",
            lambda: sf,
        )
        from app.gateway.tenant import resolve_tenant_context

        with pytest.raises(RuntimeError, match="no active OrgMembership"):
            await resolve_tenant_context(_fake_user(user_id="u-orphan"), "session", "req-1", SimpleNamespace(headers={}))

    @pytest.mark.anyio
    async def test_multi_membership_raises(self, sf, monkeypatch):
        await _seed_org(sf, org_id="o-1")
        await _seed_org(sf, org_id="o-2")
        await _seed_membership(sf, org_id="o-1", user_id="u-1")
        await _seed_membership(sf, org_id="o-2", user_id="u-1")
        _set_phase(monkeypatch, "validation")
        monkeypatch.setattr(
            "deerflow.persistence.engine.get_session_factory",
            lambda: sf,
        )
        from app.gateway.tenant import resolve_tenant_context

        with pytest.raises(MultiMembershipError):
            await resolve_tenant_context(_fake_user(user_id="u-1"), "session", "req-1", SimpleNamespace(headers={}))

    @pytest.mark.anyio
    async def test_sf_none_raises_in_multi_org_phase(self, monkeypatch):
        # backend=memory returns None from get_session_factory; multi-org phases
        # must fail closed rather than fabricate a default org.
        _set_phase(monkeypatch, "active")
        monkeypatch.setattr(
            "deerflow.persistence.engine.get_session_factory",
            lambda: None,
        )
        from app.gateway.tenant import resolve_tenant_context

        with pytest.raises(RuntimeError, match="requires persistence"):
            await resolve_tenant_context(_fake_user(), "session", "req-1", SimpleNamespace(headers={}))
