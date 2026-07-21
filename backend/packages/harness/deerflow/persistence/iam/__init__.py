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
    create_role_binding,
    create_service_account,
    delete_role_binding,
    delete_service_account,
    get_service_account,
    list_role_bindings,
    list_service_accounts,
    set_service_account_status,
    update_service_account,
)

__all__ = [
    # ORM models
    "ApiKeyRow",
    "RoleBindingRow",
    "RoleRow",
    "ServiceAccountRow",
    # repository (PR-034)
    "SERVICE_ACCOUNT_ACTIVE",
    "SERVICE_ACCOUNT_DISABLED",
    "create_role_binding",
    "create_service_account",
    "delete_role_binding",
    "delete_service_account",
    "get_service_account",
    "list_role_bindings",
    "list_service_accounts",
    "set_service_account_status",
    "update_service_account",
]
