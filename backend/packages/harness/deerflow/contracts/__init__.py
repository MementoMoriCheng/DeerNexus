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
* PR-011 (this commit) — RunEnvelope, PolicySnapshotRef, EnvelopeIntegrity,
  Policy (Request/Decision/Obligation/Evaluator), ReleaseRef/Resolver,
  ApprovalTicket, AuditEvent/AuditSink, UsageRecord/UsageRecorder + fixtures.
* PR-012 — TenantContext ContextVar lifecycle helpers.
"""

from deerflow.contracts.approval import ApprovalStatus, ApprovalTicket
from deerflow.contracts.context import AuthMethod, TenantContext
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
]
