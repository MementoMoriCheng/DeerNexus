"""Unit tests for the PR-030 permission registry and builtin Org roles.

Covers the frozen :class:`Permission` registry (RBAC-030-REGISTRY), the three
builtin Org roles and their permission matrices (RBAC-030-ROLES), the
``system`` permission isolation rule (RBAC-030-SYSTEM), and the matrix in
``docs/engineering/testing-strategy.md`` §9.1 (RBAC-030-MATRIX). These are
pure contract tests — no app / ORM / FastAPI dependency.

The ServiceAccount column of the §9.1 matrix is deferred to PR-034
(``按 scope`` semantics need API Key scopes, which PR-030 does not deliver).
"""

from __future__ import annotations

import pytest

from deerflow.contracts import (
    BUILTIN_ROLE_NAMES,
    BUILTIN_ROLE_PERMISSIONS,
    BUILTIN_ROLE_TEMPLATE_VERSION,
    ORG_ADMIN_ROLE_NAME,
    ORG_DEVELOPER_ROLE_NAME,
    ORG_VIEWER_ROLE_NAME,
    SYSTEM_PERMISSIONS,
    Permission,
    PermissionValidationError,
    validate_role_permissions,
)

# ---------------------------------------------------------------------------
# Expected sets — kept as module-level literals so a drift in either the
# registry or this file shows up as a set-equality failure with a clear diff,
# not a silent parametrize skip. Source of truth: ADR-0003 §3.
# ---------------------------------------------------------------------------

_EXPECTED_PERMISSIONS = {
    # runtime
    "runtime:thread:read",
    "runtime:thread:write",
    "runtime:run:create",
    "runtime:run:read",
    "runtime:run:cancel",
    "runtime:run:resume",
    # admin
    "admin:org:read",
    "admin:org:manage",
    "admin:iam:read",
    "admin:iam:manage",
    "admin:audit:read",
    "admin:console:read",
    # studio
    "studio:package:read",
    "studio:package:write",
    "studio:release:promote_dev",
    "studio:release:promote",
    "studio:release:rollback",
    # connector
    "connector:read",
    "connector:manage",
    # system
    "system:org:create",
    "system:org:read_all",
    "system:org:operate_all",
}

_EXPECTED_SYSTEM_PERMISSIONS = {
    "system:org:create",
    "system:org:read_all",
    "system:org:operate_all",
}


class TestPermissionRegistry:
    """RBAC-030-REGISTRY — the 22 MVP permission strings are frozen."""

    def test_registry_matches_mvp_set(self):
        # Guard against silent drift: every permission in ADR-0003 §3 must be
        # present exactly once, with its canonical three-segment string value.
        assert {p.value for p in Permission} == _EXPECTED_PERMISSIONS
        assert len(Permission) == 22

    def test_every_permission_has_two_or_three_segments(self):
        # ADR-0003 §3 declares format <domain>:<resource>:<action> (two
        # colons), but the ``connector`` domain is an intentional two-segment
        # shorthand (``connector:read`` / ``connector:manage``) that the ADR
        # §3 permission list itself uses. Accept both shapes; the registry is
        # the authority, not a colon-count regex.
        for perm in Permission:
            assert perm.value.count(":") in (1, 2), perm

    def test_every_permission_has_known_domain(self):
        allowed_domains = {"runtime", "admin", "studio", "connector", "system"}
        for perm in Permission:
            assert perm.value.split(":", 1)[0] in allowed_domains, perm

    def test_system_permissions_derived_set(self):
        # SYSTEM_PERMISSIONS is derived from the prefix; assert it matches the
        # explicit expectation so the isolation rule has a single anchor.
        assert {p.value for p in SYSTEM_PERMISSIONS} == _EXPECTED_SYSTEM_PERMISSIONS
        assert len(SYSTEM_PERMISSIONS) == 3

    def test_str_enum_round_trips_through_string(self):
        # StrEnum: member == its string value; this is how permissions are
        # serialized into roles.permissions JSON.
        assert Permission.RUNTIME_RUN_CREATE == "runtime:run:create"
        assert str(Permission.RUNTIME_RUN_CREATE) == "runtime:run:create"


class TestBuiltinRoles:
    """RBAC-030-ROLES — three builtin Org roles with ADR-0003 §4 matrices."""

    def test_three_builtin_roles_present(self):
        assert BUILTIN_ROLE_NAMES == frozenset({ORG_ADMIN_ROLE_NAME, ORG_DEVELOPER_ROLE_NAME, ORG_VIEWER_ROLE_NAME})
        assert BUILTIN_ROLE_NAMES == frozenset(BUILTIN_ROLE_PERMISSIONS)

    def test_role_names_match_data_model(self):
        # data-model.md §5.1 + ADR-0003 §4 fix these names.
        assert ORG_ADMIN_ROLE_NAME == "org:admin"
        assert ORG_DEVELOPER_ROLE_NAME == "org:developer"
        assert ORG_VIEWER_ROLE_NAME == "org:viewer"

    def test_admin_permissions_match_adr(self):
        expected = {
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
        assert BUILTIN_ROLE_PERMISSIONS[ORG_ADMIN_ROLE_NAME] == expected
        assert len(BUILTIN_ROLE_PERMISSIONS[ORG_ADMIN_ROLE_NAME]) == 19

    def test_developer_permissions_match_adr(self):
        expected = {
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
        assert BUILTIN_ROLE_PERMISSIONS[ORG_DEVELOPER_ROLE_NAME] == expected
        assert len(BUILTIN_ROLE_PERMISSIONS[ORG_DEVELOPER_ROLE_NAME]) == 10

    def test_viewer_permissions_match_adr(self):
        expected = {
            Permission.RUNTIME_THREAD_READ,
            Permission.RUNTIME_RUN_READ,
            Permission.STUDIO_PACKAGE_READ,
            Permission.CONNECTOR_READ,
        }
        assert BUILTIN_ROLE_PERMISSIONS[ORG_VIEWER_ROLE_NAME] == expected
        assert len(BUILTIN_ROLE_PERMISSIONS[ORG_VIEWER_ROLE_NAME]) == 4

    def test_role_mappings_are_frozen(self):
        # frozenset values: a downstream mutation must not silently drift the
        # seed. If this fails, someone replaced frozenset with set.
        for name, perms in BUILTIN_ROLE_PERMISSIONS.items():
            assert isinstance(perms, frozenset), name

    def test_template_version_is_positive_int(self):
        # ADR-0003 §5: template changes require a migration + version bump.
        assert isinstance(BUILTIN_ROLE_TEMPLATE_VERSION, int)
        assert BUILTIN_ROLE_TEMPLATE_VERSION >= 1


class TestSystemIsolation:
    """RBAC-030-SYSTEM — system:* never reaches a builtin Org role."""

    def test_no_builtin_role_carries_system_permission(self):
        # ADR-0003 §3 constraint. All three builtin roles are Org-scoped
        # templates; none may carry a platform-level permission.
        for role_name, perms in BUILTIN_ROLE_PERMISSIONS.items():
            assert perms.isdisjoint(SYSTEM_PERMISSIONS), role_name

    def test_system_permissions_are_subset_of_registry(self):
        # Sanity: SYSTEM_PERMISSIONS is derived from Permission, so it is by
        # construction a subset; this guards against a future refactor that
        # decouples them.
        assert SYSTEM_PERMISSIONS.issubset(frozenset(Permission))


class TestValidateRolePermissions:
    """RBAC-030-VALIDATE — write-side guard for custom-role paths."""

    @pytest.mark.parametrize(
        "perms",
        [
            [Permission.RUNTIME_THREAD_READ],
            [Permission.RUNTIME_THREAD_READ, Permission.ADMIN_AUDIT_READ],
            list(BUILTIN_ROLE_PERMISSIONS[ORG_VIEWER_ROLE_NAME]),
            [],  # empty is valid (a role may be inert)
        ],
    )
    def test_valid_org_role_permissions_pass(self, perms):
        # Known permissions, no system prefix → accepted for Org-scoped roles.
        validate_role_permissions(perms, is_system=False)  # no raise

    @pytest.mark.parametrize(
        "perms",
        [
            [Permission.SYSTEM_ORG_CREATE],
            [Permission.SYSTEM_ORG_READ_ALL, Permission.SYSTEM_ORG_OPERATE_ALL],
            [Permission.RUNTIME_THREAD_READ, Permission.SYSTEM_ORG_CREATE],
        ],
    )
    def test_system_permission_in_org_role_rejected(self, perms):
        # ADR-0003 §3: system permissions cannot be granted to an Org role.
        with pytest.raises(PermissionValidationError) as exc_info:
            validate_role_permissions(perms, is_system=False)
        assert exc_info.value.code.value == "validation_error"
        assert "system" in str(exc_info.value).lower()

    def test_system_permission_in_system_role_allowed(self):
        # A future system-template role may carry system permissions; the guard
        # only applies to Org-scoped (is_system=False) roles.
        validate_role_permissions(
            [Permission.SYSTEM_ORG_CREATE, Permission.SYSTEM_ORG_READ_ALL],
            is_system=True,
        )  # no raise

    @pytest.mark.parametrize(
        "perms",
        [
            ["runtime:thread:read", "runtime:bogus:action"],
            ["admin:org:read", "totally:made:up"],
            ["unknown"],
            [""],
        ],
    )
    def test_unknown_permission_string_rejected(self, perms):
        # ADR-0003 §2: unknown permission strings are not auto-granted.
        with pytest.raises(PermissionValidationError) as exc_info:
            validate_role_permissions(perms, is_system=False)
        assert exc_info.value.code.value == "validation_error"

    def test_accepts_permission_members_or_strings(self):
        # The function must accept both Permission members and raw strings so
        # callers do not need to coerce.
        validate_role_permissions(
            [Permission.RUNTIME_THREAD_READ, "runtime:run:read"],
            is_system=False,
        )  # no raise

    def test_rejects_string_not_in_registry(self):
        # A string not in the frozen registry is rejected; the registry is
        # the authority, not a format regex.
        with pytest.raises(PermissionValidationError):
            validate_role_permissions(["runtime:read"], is_system=False)


# ---------------------------------------------------------------------------
# RBAC-030-MATRIX — testing-strategy.md §9.1, Admin/Developer/Viewer columns.
# ServiceAccount column (按 scope) is deferred to PR-034.
# ---------------------------------------------------------------------------


def _role_perms(role_name: str) -> set[str]:
    return {p.value for p in BUILTIN_ROLE_PERMISSIONS[role_name]}


class TestRbacMatrix:
    """Executes the 9×3 grid from testing-strategy.md §9.1.

    Each cell asserts whether the role's permission set grants the capability.
    "允许" → permission present; "拒绝" / "默认拒绝" → permission absent
    (the distinction between explicit deny and default deny is a PR-031
    runtime concern; at the registry level both look like absence).
    """

    # Read Thread / Run
    def test_admin_can_read_thread_run(self):
        perms = _role_perms(ORG_ADMIN_ROLE_NAME)
        assert Permission.RUNTIME_THREAD_READ.value in perms
        assert Permission.RUNTIME_RUN_READ.value in perms

    def test_developer_can_read_thread_run(self):
        perms = _role_perms(ORG_DEVELOPER_ROLE_NAME)
        assert Permission.RUNTIME_THREAD_READ.value in perms
        assert Permission.RUNTIME_RUN_READ.value in perms

    def test_viewer_can_read_thread_run(self):
        perms = _role_perms(ORG_VIEWER_ROLE_NAME)
        assert Permission.RUNTIME_THREAD_READ.value in perms
        assert Permission.RUNTIME_RUN_READ.value in perms

    # Create Run
    @pytest.mark.parametrize(
        ("role", "allowed"),
        [
            (ORG_ADMIN_ROLE_NAME, True),
            (ORG_DEVELOPER_ROLE_NAME, True),
            (ORG_VIEWER_ROLE_NAME, False),
        ],
    )
    def test_create_run(self, role, allowed):
        perms = _role_perms(role)
        assert (Permission.RUNTIME_RUN_CREATE.value in perms) is allowed

    # Cancel / Resume
    @pytest.mark.parametrize(
        ("role", "allowed"),
        [
            (ORG_ADMIN_ROLE_NAME, True),
            (ORG_DEVELOPER_ROLE_NAME, True),
            (ORG_VIEWER_ROLE_NAME, False),
        ],
    )
    def test_cancel_resume_run(self, role, allowed):
        perms = _role_perms(role)
        assert (Permission.RUNTIME_RUN_CANCEL.value in perms) is allowed
        assert (Permission.RUNTIME_RUN_RESUME.value in perms) is allowed

    # Console
    @pytest.mark.parametrize(
        ("role", "allowed"),
        [
            (ORG_ADMIN_ROLE_NAME, True),
            (ORG_DEVELOPER_ROLE_NAME, False),
            (ORG_VIEWER_ROLE_NAME, False),
        ],
    )
    def test_console(self, role, allowed):
        perms = _role_perms(role)
        assert (Permission.ADMIN_CONSOLE_READ.value in perms) is allowed

    # Membership / Role (IAM manage)
    @pytest.mark.parametrize(
        ("role", "allowed"),
        [
            (ORG_ADMIN_ROLE_NAME, True),
            (ORG_DEVELOPER_ROLE_NAME, False),
            (ORG_VIEWER_ROLE_NAME, False),
        ],
    )
    def test_membership_role_management(self, role, allowed):
        perms = _role_perms(role)
        assert (Permission.ADMIN_IAM_MANAGE.value in perms) is allowed

    # Agent Draft (read vs write split for Viewer)
    @pytest.mark.parametrize(
        ("role", "can_read", "can_write"),
        [
            (ORG_ADMIN_ROLE_NAME, True, True),
            (ORG_DEVELOPER_ROLE_NAME, True, True),
            (ORG_VIEWER_ROLE_NAME, True, False),  # 只读
        ],
    )
    def test_agent_draft(self, role, can_read, can_write):
        perms = _role_perms(role)
        assert (Permission.STUDIO_PACKAGE_READ.value in perms) is can_read
        assert (Permission.STUDIO_PACKAGE_WRITE.value in perms) is can_write

    # dev Promote
    @pytest.mark.parametrize(
        ("role", "allowed"),
        [
            (ORG_ADMIN_ROLE_NAME, True),
            (ORG_DEVELOPER_ROLE_NAME, True),
            (ORG_VIEWER_ROLE_NAME, False),
        ],
    )
    def test_dev_promote(self, role, allowed):
        perms = _role_perms(role)
        assert (Permission.STUDIO_RELEASE_PROMOTE_DEV.value in perms) is allowed

    # prod Promote / Rollback
    @pytest.mark.parametrize(
        ("role", "allowed"),
        [
            (ORG_ADMIN_ROLE_NAME, True),
            (ORG_DEVELOPER_ROLE_NAME, False),
            (ORG_VIEWER_ROLE_NAME, False),
        ],
    )
    def test_prod_promote_rollback(self, role, allowed):
        perms = _role_perms(role)
        assert (Permission.STUDIO_RELEASE_PROMOTE.value in perms) is allowed
        assert (Permission.STUDIO_RELEASE_ROLLBACK.value in perms) is allowed

    # Audit query
    @pytest.mark.parametrize(
        ("role", "allowed"),
        [
            (ORG_ADMIN_ROLE_NAME, True),
            (ORG_DEVELOPER_ROLE_NAME, False),
            (ORG_VIEWER_ROLE_NAME, False),
        ],
    )
    def test_audit_query(self, role, allowed):
        perms = _role_perms(role)
        assert (Permission.ADMIN_AUDIT_READ.value in perms) is allowed
