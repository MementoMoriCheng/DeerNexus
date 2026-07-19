"""DeerNexus runtime contracts.

Stable DTOs, error codes and Protocols that define the boundary between the
DeerFlow runtime kernel (the ``deerflow`` harness) and the DeerNexus control
plane (``app``). The harness depends on these contracts; control-plane adapters
implement the Protocols declared here. Dependency direction:

    deerflow runtime  ->  deerflow.contracts  <-  app.control_plane adapters

Contracts depend only on the Python standard library and Pydantic base types.
They must never import ORM models, FastAPI routers, LangGraph/LangChain or any
control-plane service. This boundary is enforced by
``backend/tests/test_harness_boundary.py``.

Authoritative spec: ``docs/architecture/runtime-contracts.md``.

Phased rollout (``docs/engineering/pr-split-guide.md`` Track A):

* PR-010 — PrincipalRef, TenantContext DTO, ContractError + error code registry,
  and canonical JSON fixtures.
* PR-011 — RunEnvelope, PolicySnapshotRef, EnvelopeIntegrity,
  Policy (Request/Decision/Obligation/Evaluator), ReleaseRef/Resolver,
  ApprovalTicket, AuditEvent/AuditSink, UsageRecord/UsageRecorder + fixtures.
* PR-012 — TenantContext ContextVar lifecycle helpers
  (bind/get/require/reset + TenantContextError).
* PR-030 — Permission registry + builtin Org roles (ADR-0003 §3-§5).
"""

from deerflow.contracts.approval import ApprovalStatus, ApprovalTicket
from deerflow.contracts.context import (
    AUTO_ORG,
    AuthMethod,
    TenantContext,
    TenantContextError,
    _OrgIdSentinel,  # noqa: F401  (re-exported for repo type annotations; mirrors deerflow.runtime.user_context._AutoSentinel)
    bind_tenant_context,
    get_tenant_context,
    require_tenant_context,
    reset_tenant_context,
    resolve_org_id,
)
from deerflow.contracts.errors import ContractError, ErrorCode, is_retryable_code
from deerflow.contracts.events import (
    AuditEvent,
    AuditOutcome,
    AuditSink,
    UsageRecord,
    UsageRecorder,
    UsageStatus,
)
from deerflow.contracts.identity import PrincipalRef, PrincipalType
from deerflow.contracts.policy import (
    Decision,
    ObligationType,
    PolicyDecision,
    PolicyEvaluator,
    PolicyObligation,
    PolicyRequest,
    ResourceRef,
    RiskClass,
)
from deerflow.contracts.rbac import (
    BUILTIN_ROLE_NAMES,
    BUILTIN_ROLE_PERMISSIONS,
    BUILTIN_ROLE_TEMPLATE_VERSION,
    ORG_ADMIN_ROLE_NAME,
    ORG_DEVELOPER_ROLE_NAME,
    ORG_VIEWER_ROLE_NAME,
    SYSTEM_PERMISSION_PREFIX,
    SYSTEM_PERMISSIONS,
    Permission,
    PermissionValidationError,
    validate_role_permissions,
)
from deerflow.contracts.release import ReleaseChannel, ReleaseRef, ReleaseResolver
from deerflow.contracts.runs import (
    EnvelopeIntegrity,
    EnvelopeSource,
    IntegrityAlgorithm,
    PolicySnapshotRef,
    RunEnvelope,
)
from deerflow.contracts.versioning import CURRENT_SCHEMA_VERSION

__all__ = [
    # versioning
    "CURRENT_SCHEMA_VERSION",
    # identity
    "PrincipalRef",
    "PrincipalType",
    # context
    "TenantContext",
    "AuthMethod",
    "TenantContextError",
    "bind_tenant_context",
    "get_tenant_context",
    "require_tenant_context",
    "reset_tenant_context",
    "AUTO_ORG",
    "resolve_org_id",
    # errors
    "ContractError",
    "ErrorCode",
    "is_retryable_code",
    # policy
    "ResourceRef",
    "PolicyRequest",
    "PolicyDecision",
    "PolicyObligation",
    "PolicyEvaluator",
    "RiskClass",
    "Decision",
    "ObligationType",
    # release
    "ReleaseRef",
    "ReleaseResolver",
    "ReleaseChannel",
    # runs / envelope
    "RunEnvelope",
    "PolicySnapshotRef",
    "EnvelopeIntegrity",
    "EnvelopeSource",
    "IntegrityAlgorithm",
    # approval (MVP reservation)
    "ApprovalTicket",
    "ApprovalStatus",
    # events
    "AuditEvent",
    "AuditSink",
    "AuditOutcome",
    "UsageRecord",
    "UsageRecorder",
    "UsageStatus",
    # rbac (PR-030)
    "Permission",
    "PermissionValidationError",
    "BUILTIN_ROLE_NAMES",
    "BUILTIN_ROLE_PERMISSIONS",
    "BUILTIN_ROLE_TEMPLATE_VERSION",
    "ORG_ADMIN_ROLE_NAME",
    "ORG_DEVELOPER_ROLE_NAME",
    "ORG_VIEWER_ROLE_NAME",
    "SYSTEM_PERMISSION_PREFIX",
    "SYSTEM_PERMISSIONS",
    "validate_role_permissions",
]
