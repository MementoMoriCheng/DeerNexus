"""Tenant bootstrap + backfill + Feature Flag helpers (PR-022 / PR-023 / PR-025B).

Idempotent seeding of the default Organization and the initial admin tenant
relationships (OrgMembership + system-template ``org:admin`` Role +
RoleBinding) — see :mod:`deerflow.tenancy.bootstrap`. Default-Org backfill
of legacy NULL ``org_id`` resource rows — see
:mod:`deerflow.tenancy.backfill`. High-risk Feature Flag registry + live
``multi_org`` phase accessor — see :mod:`deerflow.tenancy.feature_flags`.
"""

from deerflow.tenancy.audit_events import emit_tenant_event
from deerflow.tenancy.backfill import BackfillReport, backfill_resource_org
from deerflow.tenancy.bootstrap import (
    SYSTEM_ADMIN_ROLE_NAME,
    ensure_admin_membership,
    ensure_admin_role_binding,
    ensure_default_org,
    ensure_system_admin_role,
    ensure_validation_org,
)
from deerflow.tenancy.feature_flags import (
    MULTI_ORG_FLAG,
    FeatureFlag,
    current_multi_org_phase,
    get_feature_flag,
    get_feature_flags,
)

__all__ = [
    "BackfillReport",
    "FeatureFlag",
    "MULTI_ORG_FLAG",
    "SYSTEM_ADMIN_ROLE_NAME",
    "backfill_resource_org",
    "current_multi_org_phase",
    "emit_tenant_event",
    "ensure_admin_membership",
    "ensure_admin_role_binding",
    "ensure_default_org",
    "ensure_system_admin_role",
    "ensure_validation_org",
    "get_feature_flag",
    "get_feature_flags",
]
