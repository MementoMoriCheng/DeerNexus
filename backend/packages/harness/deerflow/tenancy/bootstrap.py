"""Idempotent tenant-skeleton bootstrap helpers (PR-022).

Materialises the "default Organization" that the single-Org tenant resolver
(PR-013/014, ``app/gateway/config.py::default_org_id``) already binds every
request / channel dispatch to, plus the initial admin tenant relationships
(OrgMembership + system-template ``org:admin`` Role + RoleBinding).

Two-phase delivery, matching ``pr-split-guide.md`` §7 ("创建默认 Org；初始
Membership / Admin；安全 bootstrap；幂等"):

* **Phase 1 (startup lifespan)** — :func:`ensure_default_org` and
  :func:`ensure_system_admin_role`. Neither has an inbound FK, so both are
  safe to create before any user exists.
* **Phase 2 (``/initialize`` first-admin creation)** —
  :func:`ensure_admin_membership` and :func:`ensure_admin_role_binding`.
  These reference a just-created ``users`` row, so they run only once an
  admin ``UserRow`` actually exists (the lifespan deliberately does NOT
  create users).

All four helpers are idempotent: they probe before insert and no-op on an
existing row, so re-runs (restart, repeated ``/initialize`` after the
``count_admin_users`` gate, or concurrent callers bypassing the bootstrap
lock) converge without raising. Each helper opens its own ``AsyncSession``
from the supplied ``async_sessionmaker`` so parent rows are committed before
child rows are added — the SQLite FK-at-commit hygiene established by
``test_tenant_schema.py``.

This module lives in the harness layer and imports only ``deerflow.persistence``
plus stdlib; it never imports from ``app`` (harness-boundary test). Org id /
slug / name are passed in by the caller (the app layer owns config).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.iam.model import RoleBindingRow, RoleRow
from deerflow.persistence.orgs.model import OrganizationRow, OrgMembershipRow
from deerflow.tenancy.audit_events import emit_tenant_event

# Canonical MVP admin role name (data-model.md §5.1). Seeded as a system
# template (org_id NULL) so it is not coupled to a specific org instance;
# PR-030 delivers the formal permission enum and org:developer / org:viewer.
SYSTEM_ADMIN_ROLE_NAME = "org:admin"


def _new_id() -> str:
    """Generate a 36-char hex id matching the String(36) convention."""
    return uuid.uuid4().hex


async def ensure_default_org(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    slug: str,
    name: str,
) -> OrganizationRow:
    """Idempotently create the default Organization row.

    Probes by ``id``; if present returns the existing row unchanged (no
    slug/name overwrite — a deployment may have renamed it). Otherwise
    inserts ``status="active"`` and commits.
    """
    async with sf() as session:
        existing = await session.get(OrganizationRow, org_id)
        if existing is not None:
            emit_tenant_event(
                "default_org_exists",
                org_id=org_id,
                principal_id=None,
                payload={"slug": existing.slug, "name": existing.name},
            )
            return existing

        row = OrganizationRow(id=org_id, slug=slug, name=name, status="active")
        session.add(row)
        await session.commit()
        await session.refresh(row)

    emit_tenant_event(
        "default_org_created",
        org_id=org_id,
        principal_id=None,
        payload={"slug": slug, "name": name},
    )
    return row


async def ensure_system_admin_role(
    sf: async_sessionmaker[AsyncSession],
    *,
    name: str = SYSTEM_ADMIN_ROLE_NAME,
) -> RoleRow:
    """Idempotently create the system-template ``org:admin`` role.

    ``is_system=True`` with ``org_id=None`` satisfies the
    ``ck_roles_system_template_allows_null_org`` CHECK and is excluded from
    the ``uq_roles_org_name`` partial unique index (``org_id IS NOT NULL``),
    so it cannot collide with future tenant roles of the same name.
    ``permissions=[]`` — the formal permission enum is PR-030's deliverable.
    """
    async with sf() as session:
        stmt = select(RoleRow).where(RoleRow.name == name, RoleRow.is_system.is_(True))
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing

        row = RoleRow(id=_new_id(), org_id=None, name=name, is_system=True, permissions=[])
        session.add(row)
        await session.commit()
        await session.refresh(row)

    emit_tenant_event(
        "system_admin_role_created",
        org_id=None,
        principal_id=None,
        payload={"role_id": row.id, "name": name},
    )
    return row


async def ensure_admin_membership(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    user_id: str,
) -> OrgMembershipRow:
    """Idempotently create an active OrgMembership for the admin user.

    Requires the ``organizations`` and ``users`` parent rows to already be
    committed (both are FK CASCADE targets). Probes by ``(org_id, user_id)``
    unique constraint; sets ``status="active"`` (data-model.md §4.5: only an
    active Membership may bind a TenantContext).
    """
    async with sf() as session:
        stmt = select(OrgMembershipRow).where(
            OrgMembershipRow.org_id == org_id,
            OrgMembershipRow.user_id == user_id,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing

        row = OrgMembershipRow(id=_new_id(), org_id=org_id, user_id=user_id, status="active")
        session.add(row)
        await session.commit()
        await session.refresh(row)

    emit_tenant_event(
        "admin_membership_created",
        org_id=org_id,
        principal_id=user_id,
        payload={"membership_id": row.id, "status": "active"},
    )
    return row


async def ensure_admin_role_binding(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    user_id: str,
    role_id: str,
) -> RoleBindingRow:
    """Idempotently bind the admin user to the system admin role in the org.

    ``role_id`` is a real FK→``roles.id`` (CASCADE) and must already be
    committed; ``org_id`` / ``principal_id`` are soft references (data-model
    §5.2, no FK). Probes by the ``(org_id, principal_type, principal_id,
    role_id)`` unique constraint.
    """
    async with sf() as session:
        stmt = select(RoleBindingRow).where(
            RoleBindingRow.org_id == org_id,
            RoleBindingRow.principal_type == "user",
            RoleBindingRow.principal_id == user_id,
            RoleBindingRow.role_id == role_id,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing

        row = RoleBindingRow(
            id=_new_id(),
            org_id=org_id,
            principal_type="user",
            principal_id=user_id,
            role_id=role_id,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

    emit_tenant_event(
        "admin_role_binding_created",
        org_id=org_id,
        principal_id=user_id,
        payload={"binding_id": row.id, "role_id": role_id},
    )
    return row


__all__ = [
    "SYSTEM_ADMIN_ROLE_NAME",
    "ensure_admin_membership",
    "ensure_admin_role_binding",
    "ensure_default_org",
    "ensure_system_admin_role",
]
