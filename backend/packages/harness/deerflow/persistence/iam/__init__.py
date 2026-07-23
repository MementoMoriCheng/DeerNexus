"""IAM control-plane ORM models and repository (PR-020B / PR-034 / PR-036 / PR-037).

Re-exports the row classes so ``deerflow.persistence.models`` can register
them with ``Base.metadata`` in a single import, and the repository helpers so
the app layer can mutate ServiceAccount / RoleBinding / OIDC-group-mapping /
OrgMembership rows without importing ``persistence.iam.repository`` directly.
"""

from deerflow.persistence.iam.model import (
    ApiKeyRow,
    OidcGroupMappingRow,
    RoleBindingRow,
    RoleRow,
    ServiceAccountRow,
)
from deerflow.persistence.iam.repository import (
    MAPPING_MODE_ADDITIVE,
    MAPPING_MODE_AUTHORITATIVE,
    MEMBERSHIP_ACTIVE,
    MEMBERSHIP_SUSPENDED,
    SERVICE_ACCOUNT_ACTIVE,
    SERVICE_ACCOUNT_DISABLED,
    count_user_bindings_for_role,
    create_api_key,
    create_oidc_group_mapping,
    create_role_binding,
    create_service_account,
    delete_oidc_group_mapping,
    delete_role_binding,
    delete_service_account,
    get_api_key,
    get_api_key_by_prefix,
    get_membership,
    get_oidc_group_mapping,
    get_service_account,
    list_api_keys,
    list_oidc_group_mappings,
    list_role_bindings,
    list_service_accounts,
    revoke_api_key,
    set_membership_status,
    set_service_account_status,
    touch_api_key_last_used,
    update_oidc_group_mapping,
    update_service_account,
)

__all__ = [
    # ORM models
    "ApiKeyRow",
    "OidcGroupMappingRow",
    "RoleBindingRow",
    "RoleRow",
    "ServiceAccountRow",
    # repository (PR-034 / PR-035 / PR-036 / PR-037)
    "MAPPING_MODE_ADDITIVE",
    "MAPPING_MODE_AUTHORITATIVE",
    "MEMBERSHIP_ACTIVE",
    "MEMBERSHIP_SUSPENDED",
    "SERVICE_ACCOUNT_ACTIVE",
    "SERVICE_ACCOUNT_DISABLED",
    "count_user_bindings_for_role",
    "create_api_key",
    "create_oidc_group_mapping",
    "create_role_binding",
    "create_service_account",
    "delete_oidc_group_mapping",
    "delete_role_binding",
    "delete_service_account",
    "get_api_key",
    "get_api_key_by_prefix",
    "get_membership",
    "get_oidc_group_mapping",
    "get_service_account",
    "list_api_keys",
    "list_oidc_group_mappings",
    "list_role_bindings",
    "list_service_accounts",
    "revoke_api_key",
    "set_membership_status",
    "set_service_account_status",
    "touch_api_key_last_used",
    "update_oidc_group_mapping",
    "update_service_account",
]
