"""Tenant bootstrap + backfill + Feature Flag + membership helpers (PR-022 / PR-023 / PR-025B / PR-025C+ / PR-036).

Idempotent seeding of the default Organization and the initial admin tenant
relationships (OrgMembership + system-template ``org:admin`` Role +
RoleBinding) — see :mod:`deerflow.tenancy.bootstrap`. Default-Org backfill
of legacy NULL ``org_id`` resource rows — see
:mod:`deerflow.tenancy.backfill`. High-risk Feature Flag registry + live
``multi_org`` phase accessor — see :mod:`deerflow.tenancy.feature_flags`.
Read-side membership lookup for request-path tenant resolution — see
:mod:`deerflow.tenancy.membership`. OIDC group → Role mapping engine +
last-admin policy primitive (ADR-0003 §10/§7) — see
:mod:`deerflow.tenancy.oidc_group_mapping`.
"""

from deerflow.tenancy.audit_events import emit_tenant_event
from deerflow.tenancy.backfill import BackfillReport, backfill_resource_org
from deerflow.tenancy.bootstrap import (
    SYSTEM_ADMIN_ROLE_NAME,
    ensure_admin_membership,
    ensure_admin_role_binding,
    ensure_builtin_roles,
    ensure_default_org,
    ensure_service_account_role_binding,
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
from deerflow.tenancy.membership import (
    MultiMembershipError,
    get_active_membership,
    get_membership_any_status,
    get_org_status,
)
from deerflow.tenancy.oidc_group_mapping import (
    GROUP_MAPPING_PROVENANCE_PREFIX,
    LastAdminError,
    MappingOutcome,
    MappingResult,
    apply_group_mapping,
    assert_not_last_admin,
    upsert_external_identity,
)

__all__ = [
    "BackfillReport",
    "FeatureFlag",
    "GROUP_MAPPING_PROVENANCE_PREFIX",
    "MULTI_ORG_FLAG",
    "MultiMembershipError",
    "LastAdminError",
    "MappingOutcome",
    "MappingResult",
    "SYSTEM_ADMIN_ROLE_NAME",
    "apply_group_mapping",
    "assert_not_last_admin",
    "backfill_resource_org",
    "current_multi_org_phase",
    "emit_tenant_event",
    "ensure_admin_membership",
    "ensure_admin_role_binding",
    "ensure_builtin_roles",
    "ensure_default_org",
    "ensure_service_account_role_binding",
    "ensure_system_admin_role",
    "ensure_validation_org",
    "get_active_membership",
    "get_feature_flag",
    "get_feature_flags",
    "get_membership_any_status",
    "get_org_status",
    "upsert_external_identity",
]
