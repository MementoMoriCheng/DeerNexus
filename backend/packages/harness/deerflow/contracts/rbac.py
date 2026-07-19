"""Stable permission registry and builtin Org roles (PR-030).

Freezes the MVP permission strings and the three builtin Org roles defined in
``docs/adr/0003-rbac-and-service-accounts.md`` §3-§5. This is the Track C
entry point: producers (lifespan seed, future role-management API) and
consumers (the PR-031 Authorize Service) reference this module as the single
authoritative source for what permission strings exist and which permissions
each builtin role carries.

What this module is, and is not:

* It **is** a frozen registry of permission strings and the builtin role →
  permission mapping, plus a pure validation function for custom-role writes.
* It is **not** a runtime authorizer. Effective-permission computation
  (``active_membership ∩ role.permissions ∩ …``) is PR-031's deliverable; this
  module only supplies the ``role.permissions`` input data.

Naming convention (ADR-0003 §3): every permission is
``<domain>:<resource>:<action>`` with one of five domains — ``runtime``,
``admin``, ``studio``, ``connector``, ``system``. The ``system`` domain is
platform-level and must never be granted to an Org-scoped role (ADR-0003 §3
constraint); :func:`validate_role_permissions` enforces that on write.

Adding a permission is a coordinated change: ADR-0003 §3, API boundaries, the
role matrix in ``docs/engineering/testing-strategy.md`` §9.1, and the
parametrized registry tests in ``backend/tests/test_contracts_rbac.py`` must
all move together (ADR-0003 §3 constraint).
"""

from __future__ import annotations

from enum import StrEnum

from deerflow.contracts.errors import ErrorCode


class Permission(StrEnum):
    """MVP permission strings (ADR-0003 §3).

    Stable contract: do not rename an existing member's string value. Adding
    a member is a compatible change as long as the four-way doc sync above is
    done in the same PR.
    """

    # domain = runtime (Run lifecycle)
    RUNTIME_THREAD_READ = "runtime:thread:read"
    RUNTIME_THREAD_WRITE = "runtime:thread:write"
    RUNTIME_RUN_CREATE = "runtime:run:create"
    RUNTIME_RUN_READ = "runtime:run:read"
    RUNTIME_RUN_CANCEL = "runtime:run:cancel"
    RUNTIME_RUN_RESUME = "runtime:run:resume"

    # domain = admin (Org governance)
    ADMIN_ORG_READ = "admin:org:read"
    ADMIN_ORG_MANAGE = "admin:org:manage"
    ADMIN_IAM_READ = "admin:iam:read"
    ADMIN_IAM_MANAGE = "admin:iam:manage"
    ADMIN_AUDIT_READ = "admin:audit:read"
    ADMIN_CONSOLE_READ = "admin:console:read"

    # domain = studio (Agent package / release)
    STUDIO_PACKAGE_READ = "studio:package:read"
    STUDIO_PACKAGE_WRITE = "studio:package:write"
    STUDIO_RELEASE_PROMOTE_DEV = "studio:release:promote_dev"
    STUDIO_RELEASE_PROMOTE = "studio:release:promote"
    STUDIO_RELEASE_ROLLBACK = "studio:release:rollback"

    # domain = connector
    CONNECTOR_READ = "connector:read"
    CONNECTOR_MANAGE = "connector:manage"

    # domain = system (platform-level, never on Org roles)
    SYSTEM_ORG_CREATE = "system:org:create"
    SYSTEM_ORG_READ_ALL = "system:org:read_all"
    SYSTEM_ORG_OPERATE_ALL = "system:org:operate_all"


#: Prefix that marks platform-level permissions excluded from Org-scoped
#: roles (ADR-0003 §3 constraint).
SYSTEM_PERMISSION_PREFIX = "system:"

#: All permissions in the ``system`` domain. Kept as a derived frozenset so
#: the isolation rule can be asserted in one place.
SYSTEM_PERMISSIONS: frozenset[Permission] = frozenset(p for p in Permission if p.value.startswith(SYSTEM_PERMISSION_PREFIX))


# Builtin role names match ``data-model.md`` §5.1 and ADR-0003 §4. They are
# system templates (org_id NULL, is_system=true), seeded once and referenced
# by RoleBinding across every Org.
ORG_ADMIN_ROLE_NAME = "org:admin"
ORG_DEVELOPER_ROLE_NAME = "org:developer"
ORG_VIEWER_ROLE_NAME = "org:viewer"

#: Names of the three builtin Org roles (ADR-0003 §4.1-§4.3). ``system:admin``
#: is intentionally absent — it is not seeded as a role row in MVP (ADR-0003
#: §4.4 keeps it independent of RoleBinding).
BUILTIN_ROLE_NAMES: frozenset[str] = frozenset({ORG_ADMIN_ROLE_NAME, ORG_DEVELOPER_ROLE_NAME, ORG_VIEWER_ROLE_NAME})

#: Builtin role → permission mapping (ADR-0003 §4.1-§4.3). Values are frozen
#: so a downstream mutation cannot silently drift the seed. Keys are the
#: ``roles.name`` strings, not permission strings — ``org:admin`` is a role
#: name that *carries* permissions, not itself a permission.
BUILTIN_ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    ORG_ADMIN_ROLE_NAME: frozenset(
        {
            Permission.RUNTIME_THREAD_READ,
            Permission.RUNTIME_THREAD_WRITE,
            Permission.RUNTIME_RUN_CREATE,
            Permission.RUNTIME_RUN_READ,
            Permission.RUNTIME_RUN_CANCEL,
            Permission.RUNTIME_RUN_RESUME,
            Permission.ADMIN_ORG_READ,
            Permission.ADMIN_ORG_MANAGE,
            Permission.ADMIN_IAM_READ,
            Permission.ADMIN_IAM_MANAGE,
            Permission.ADMIN_AUDIT_READ,
            Permission.ADMIN_CONSOLE_READ,
            Permission.STUDIO_PACKAGE_READ,
            Permission.STUDIO_PACKAGE_WRITE,
            Permission.STUDIO_RELEASE_PROMOTE_DEV,
            Permission.STUDIO_RELEASE_PROMOTE,
            Permission.STUDIO_RELEASE_ROLLBACK,
            Permission.CONNECTOR_READ,
            Permission.CONNECTOR_MANAGE,
        }
    ),
    ORG_DEVELOPER_ROLE_NAME: frozenset(
        {
            Permission.RUNTIME_THREAD_READ,
            Permission.RUNTIME_THREAD_WRITE,
            Permission.RUNTIME_RUN_CREATE,
            Permission.RUNTIME_RUN_READ,
            Permission.RUNTIME_RUN_CANCEL,
            Permission.RUNTIME_RUN_RESUME,
            Permission.STUDIO_PACKAGE_READ,
            Permission.STUDIO_PACKAGE_WRITE,
            Permission.STUDIO_RELEASE_PROMOTE_DEV,
            Permission.CONNECTOR_READ,
        }
    ),
    ORG_VIEWER_ROLE_NAME: frozenset(
        {
            Permission.RUNTIME_THREAD_READ,
            Permission.RUNTIME_RUN_READ,
            Permission.STUDIO_PACKAGE_READ,
            Permission.CONNECTOR_READ,
        }
    ),
}

#: Version of the builtin role template payload. Bumped on every seed
#: migration that changes any builtin role's permission set, so audits and
#: migrations can correlate a row with the seed revision that produced it
#: (ADR-0003 §5 "内置角色变更必须有迁移、变更记录和回归测试"). Custom roles
#: leave ``template_version`` NULL — this field only tracks system templates.
BUILTIN_ROLE_TEMPLATE_VERSION: int = 1


def _permission_value(permission: Permission | str) -> str:
    """Normalize a Permission member or raw string to its string value."""
    return permission.value if isinstance(permission, Permission) else str(permission)


class PermissionValidationError(ValueError):
    """Raised when a role's permission list violates the registry rules.

    Carries the stable :class:`~deerflow.contracts.errors.ErrorCode` so entry
    points can translate it to a :class:`ContractError` envelope without
    string matching. Mirrors :class:`~deerflow.contracts.context.TenantContextError`.
    """

    code: ErrorCode

    def __init__(self, code: ErrorCode, message: str, *, permission: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.permission = permission


def validate_role_permissions(
    permissions,
    *,
    is_system: bool,
) -> None:
    """Validate a role's permission list before it is persisted.

    Rules (ADR-0003 §2 "默认拒绝,未知权限字符串和未知角色不自动放行" + §3
    "system 权限不允许写入 Org 自定义角色"):

    * Every entry must be a known :class:`Permission` value. An unknown
      string raises :class:`PermissionValidationError`
      (code ``validation_error``) so the caller surfaces it as a 400 rather
      than silently storing noise.
    * When ``is_system`` is ``False`` (an Org-scoped role), no entry may be in
      the ``system`` domain. System permissions are platform-level only and
      must never widen an Org role.

    The function returns ``None`` on success. Builtin system templates bypass
    this check at seed time (they are the authoritative source, not subject
    to re-validation); it is intended for custom-role write paths that arrive
    in later PRs.
    """
    known_values = {p.value for p in Permission}
    for raw in permissions:
        value = _permission_value(raw)
        if value not in known_values:
            raise PermissionValidationError(
                ErrorCode.VALIDATION_ERROR,
                "Unknown permission string; not in the frozen registry.",
                permission=value,
            )
        if not is_system and value.startswith(SYSTEM_PERMISSION_PREFIX):
            raise PermissionValidationError(
                ErrorCode.VALIDATION_ERROR,
                "system permissions cannot be granted to an Org-scoped role.",
                permission=value,
            )


__all__ = [
    "BUILTIN_ROLE_NAMES",
    "BUILTIN_ROLE_PERMISSIONS",
    "BUILTIN_ROLE_TEMPLATE_VERSION",
    "ORG_ADMIN_ROLE_NAME",
    "ORG_DEVELOPER_ROLE_NAME",
    "ORG_VIEWER_ROLE_NAME",
    "Permission",
    "PermissionValidationError",
    "SYSTEM_PERMISSION_PREFIX",
    "SYSTEM_PERMISSIONS",
    "validate_role_permissions",
]
