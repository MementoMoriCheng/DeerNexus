"""Tenant bootstrap helpers (PR-022).

Idempotent seeding of the default Organization and the initial admin tenant
relationships (OrgMembership + system-template ``org:admin`` Role +
RoleBinding). See :mod:`deerflow.tenancy.bootstrap` for the two-phase
delivery rationale.
"""

from deerflow.tenancy.audit_events import emit_tenant_event
from deerflow.tenancy.bootstrap import (
    SYSTEM_ADMIN_ROLE_NAME,
    ensure_admin_membership,
    ensure_admin_role_binding,
    ensure_default_org,
    ensure_system_admin_role,
)

__all__ = [
    "SYSTEM_ADMIN_ROLE_NAME",
    "emit_tenant_event",
    "ensure_admin_membership",
    "ensure_admin_role_binding",
    "ensure_default_org",
    "ensure_system_admin_role",
]
