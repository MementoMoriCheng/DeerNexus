"""IAM control-plane ORM models and repository (PR-020B / PR-034).

Re-exports the four row classes so ``deerflow.persistence.models`` can register
them with ``Base.metadata`` in a single import, and the repository helpers so
the app layer can mutate ServiceAccount / RoleBinding rows without importing
``persistence.iam.repository`` directly.
"""

from deerflow.persistence.iam.model import (
    ApiKeyRow,
    RoleBindingRow,
    RoleRow,
    ServiceAccountRow,
)
from deerflow.persistence.iam.repository import (
    SERVICE_ACCOUNT_ACTIVE,
    SERVICE_ACCOUNT_DISABLED,
    create_api_key,
    create_role_binding,
    create_service_account,
    delete_role_binding,
    delete_service_account,
    get_api_key,
    get_api_key_by_prefix,
    get_service_account,
    list_api_keys,
    list_role_bindings,
    list_service_accounts,
    revoke_api_key,
    set_service_account_status,
    touch_api_key_last_used,
    update_service_account,
)

__all__ = [
    # ORM models
    "ApiKeyRow",
    "RoleBindingRow",
    "RoleRow",
    "ServiceAccountRow",
    # repository (PR-034 / PR-035)
    "SERVICE_ACCOUNT_ACTIVE",
    "SERVICE_ACCOUNT_DISABLED",
    "create_api_key",
    "create_role_binding",
    "create_service_account",
    "delete_role_binding",
    "delete_service_account",
    "get_api_key",
    "get_api_key_by_prefix",
    "get_service_account",
    "list_api_keys",
    "list_role_bindings",
    "list_service_accounts",
    "revoke_api_key",
    "set_service_account_status",
    "touch_api_key_last_used",
    "update_service_account",
]
