"""DeerNexus runtime contracts.

Stable DTOs and error codes that define the boundary between the DeerFlow
runtime kernel (the ``deerflow`` harness) and the DeerNexus control plane
(``app``). The harness depends on these contracts; control-plane adapters
implement the Protocols declared here. Dependency direction:

    deerflow runtime  ->  deerflow.contracts  <-  app.control_plane adapters

Contracts depend only on the Python standard library and Pydantic base types.
They must never import ORM models, FastAPI routers, LangGraph/LangChain or any
control-plane service. This boundary is enforced by
``backend/tests/test_harness_boundary.py``.

Authoritative spec: ``docs/architecture/runtime-contracts.md``.

Phased rollout (``docs/engineering/pr-split-guide.md`` Track A):

* PR-010 (this package) — PrincipalRef, TenantContext DTO, ContractError +
  error code registry, and canonical JSON fixtures.
* PR-011 — RunEnvelope, Policy / Release / Event contracts and their
  Protocols.
* PR-012 — TenantContext ContextVar lifecycle helpers.
"""

from deerflow.contracts.context import AuthMethod, TenantContext
from deerflow.contracts.errors import ContractError, ErrorCode, is_retryable_code
from deerflow.contracts.identity import PrincipalRef, PrincipalType
from deerflow.contracts.versioning import CURRENT_SCHEMA_VERSION

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "PrincipalRef",
    "PrincipalType",
    "TenantContext",
    "AuthMethod",
    "ContractError",
    "ErrorCode",
    "is_retryable_code",
]
