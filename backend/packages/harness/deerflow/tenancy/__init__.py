"""Tenant bootstrap + backfill helpers (PR-022 / PR-023).

Idempotent seeding of the default Organization and the initial admin tenant
relationships (OrgMembership + system-template ``org:admin`` Role +
RoleBinding) — see :mod:`deerflow.tenancy.bootstrap`. Default-Org backfill
of legacy NULL ``org_id`` resource rows — see
:mod:`deerflow.tenancy.backfill`.
"""

from deerflow.tenancy.audit_events import emit_tenant_event
from deerflow.tenancy.backfill import BackfillReport, backfill_resource_org
from deerflow.tenancy.bootstrap import (
    SYSTEM_ADMIN_ROLE_NAME,
    ensure_admin_membership,
    ensure_admin_role_binding,
    ensure_default_org,
    ensure_system_admin_role,
)

__all__ = [
    "BackfillReport",
    "SYSTEM_ADMIN_ROLE_NAME",
    "backfill_resource_org",
    "emit_tenant_event",
    "ensure_admin_membership",
    "ensure_admin_role_binding",
    "ensure_default_org",
    "ensure_system_admin_role",
]
