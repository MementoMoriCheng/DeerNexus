"""Rebuild TenantContext from a trusted RunEnvelope (runtime-contracts §5.2 rule 4).

The embedded Worker (and a future physical Worker) rebuilds the tenant scope
from the persisted ``RunEnvelope`` rather than relying on in-process ContextVar
inheritance. This is the trusted-task-envelope contract (ADR-0002 §3 invariant
6): async tasks must serialize needed fields into a trusted envelope, not rely
only on the ContextVar.

Today the Gateway constructs the envelope at run start and the embedded Worker
re-binds the tenant from ``envelope.tenant``. When a physical Worker split
arrives (ADR-0006), the same rebuild runs after the envelope crosses the process
boundary (with ``EnvelopeIntegrity`` verified there).

Scope: this helper rebuilds the ``TenantContext`` (the ``.tenant`` field). It
does not validate ``release_ref`` / ``policy_snapshot`` consistency — those
belong to the Release (PR-050+) and RBAC (PR-030+) tracks and are not consumed
by the tenant scope.
"""

from __future__ import annotations

from contextvars import Token

from deerflow.contracts import (
    ErrorCode,
    RunEnvelope,
    TenantContext,
    TenantContextError,
    bind_tenant_context,
)


def rebuild_tenant_context(envelope: RunEnvelope) -> TenantContext:
    """Rebuild the trusted TenantContext from a RunEnvelope.

    The envelope's ``tenant`` was set by a trusted entry point (Gateway /
    Scheduler / Channel) after authentication and org resolution; the Worker
    trusts it verbatim rather than re-deriving from a (possibly absent)
    ContextVar. Raises :class:`TenantContextError` (fail-closed) if the
    envelope or its tenant is missing — the Worker must never fall back to a
    default Org (runtime-contracts §5.2 rule 6).
    """
    if envelope is None or envelope.tenant is None:
        raise TenantContextError(
            ErrorCode.TENANT_CONTEXT_MISSING,
            "RunEnvelope carries no tenant context; cannot rebuild tenant scope",
        )
    return envelope.tenant


def bind_tenant_from_envelope(envelope: RunEnvelope) -> Token[TenantContext | None]:
    """Rebuild and bind the tenant context from a RunEnvelope.

    Convenience wrapper for Worker entry points: rebuild via
    :func:`rebuild_tenant_context` then :func:`bind_tenant_context`. The caller
    must ``reset_tenant_context(token)`` in a ``finally`` block.
    """
    tenant = rebuild_tenant_context(envelope)
    return bind_tenant_context(tenant)
