"""TenantContext contract and its ContextVar lifecycle helpers.

The immutable, tenant-scoped context consumed by the runtime kernel. Trusted
entry points (Gateway, Worker, Scheduler, Channel adapters) construct it after
authentication, organization resolution and workspace validation, then bind it
so downstream code operates inside a verified tenant scope.

This module owns both the immutable DTO and its invariants
(runtime-contracts.md §5.1) and the ContextVar lifecycle helpers
(runtime-contracts.md §5.2):

* ``bind_tenant_context(context) -> token``
* ``get_tenant_context() -> TenantContext | None``
* ``require_tenant_context() -> TenantContext``
* ``reset_tenant_context(token) -> None``

Asyncio semantics
-----------------
``ContextVar`` is task-local under asyncio, not thread-local. Each FastAPI
request runs in its own task, so the context is naturally isolated.
``asyncio.create_task`` and ``asyncio.to_thread`` inherit the parent task's
context, which is typically the intended behaviour; if a background task must
*not* see the foreground tenant, wrap it with ``contextvars.copy_context()`` to
get a clean copy. A bare ``ThreadPoolExecutor`` does *not* copy context, so
worker threads see the unset (``None``) value — tasks scheduled on plain thread
pools must thread the tenant explicitly rather than rely on the contextvar.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deerflow.contracts.errors import ErrorCode
from deerflow.contracts.identity import PrincipalRef
from deerflow.contracts.versioning import CURRENT_SCHEMA_VERSION

AuthMethod = Literal["oidc", "session", "api_key", "internal"]
"""How the principal was authenticated. Unknown values are rejected."""


class TenantContext(BaseModel):
    """Trusted tenant context for a request or trusted task.

    Invariants (runtime-contracts.md §5.1):

    * ``org_id`` is non-empty — the organization is the hard isolation
      boundary and never defaults to an implicit org;
    * the object is immutable (``frozen=True``);
    * it carries no secrets, tokens, session cookies or full OIDC claims —
      unknown fields are dropped so a leaked credential cannot ride along;
    * a client-supplied ``org_id`` is never the trusted source of truth; the
      trusted value is set by the entry point after membership resolution.

    ``issued_at`` is always timezone-aware and normalized to UTC.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str = Field(
        default=CURRENT_SCHEMA_VERSION,
        description="Contracts schema version.",
    )
    org_id: str = Field(
        min_length=1,
        description="Organization id (hard isolation boundary); never empty.",
    )
    workspace_id: str | None = Field(
        default=None,
        description="Optional workspace within org_id; must belong to org_id when set.",
    )
    principal: PrincipalRef = Field(
        description="Authenticated principal behind this context.",
    )
    auth_method: AuthMethod = Field(
        description="How the principal was authenticated.",
    )
    request_id: str = Field(
        min_length=1,
        description="Per-request correlation id; never empty.",
    )
    trace_id: str | None = Field(
        default=None,
        description="Optional distributed trace id.",
    )
    issued_at: datetime = Field(
        description="When the context was bound (UTC, RFC 3339, timezone-aware).",
    )

    @field_validator("issued_at", mode="before")
    @classmethod
    def _issued_at_utc(cls, value: object) -> datetime:
        """Require a timezone-aware timestamp and normalize it to UTC."""
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise TypeError("issued_at must be an RFC 3339 datetime")
        if value.tzinfo is None:
            raise ValueError("issued_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# ContextVar lifecycle (runtime-contracts.md §5.2)
# ---------------------------------------------------------------------------
#
# Trusted entry points (Gateway, Worker, Scheduler, Channel adapters) bind a
# TenantContext after authentication / org resolution so downstream code
# operates inside a verified tenant scope. ``bind`` must always be paired with
# ``reset`` in a ``try/finally`` so the contextvar is restored on both normal
# and exceptional exits — never falls back to a default Org when unset.

_current_tenant: Final[ContextVar[TenantContext | None]] = ContextVar(
    "deerflow_current_tenant",
    default=None,
)


class TenantContextError(RuntimeError):
    """Raised when a tenant context is required but not bound.

    Carries the stable :class:`~deerflow.contracts.errors.ErrorCode` so entry
    points can catch it and translate to a :class:`ContractError` envelope
    without string matching. The default code is
    :attr:`~deerflow.contracts.errors.ErrorCode.TENANT_CONTEXT_MISSING`
    (non-retryable) — the runtime must never recover from a missing tenant by
    falling back to a default Org.
    """

    code: ErrorCode

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def bind_tenant_context(context: TenantContext) -> Token[TenantContext | None]:
    """Bind ``context`` for the current async task / thread.

    Returns a reset token that should be passed to :func:`reset_tenant_context`
    in a ``finally`` block to restore the previous context. Use ``try/finally``
    so the contextvar is restored on both normal and exceptional exits.
    """
    return _current_tenant.set(context)


def reset_tenant_context(token: Token[TenantContext | None]) -> None:
    """Restore the context to the state captured by ``token``."""
    _current_tenant.reset(token)


def get_tenant_context() -> TenantContext | None:
    """Return the current tenant context, or ``None`` if unset.

    Safe to call in any context. Never synthesizes a default Org when unset —
    callers that need a tenant must use :func:`require_tenant_context` so a
    missing context fails closed.
    """
    return _current_tenant.get()


def require_tenant_context() -> TenantContext:
    """Return the current tenant context, or raise :class:`TenantContextError`.

    Used by code that must not run outside a tenant-authenticated scope. Raises
    with :attr:`ErrorCode.TENANT_CONTEXT_MISSING` when nothing is bound — the
    runtime must never fall back to a default Org (fail closed).
    """
    context = _current_tenant.get()
    if context is None:
        raise TenantContextError(
            ErrorCode.TENANT_CONTEXT_MISSING,
            "tenant context not bound in current task; bind via bind_tenant_context in a try/finally at the trusted entry point",
        )
    return context


# ---------------------------------------------------------------------------
# Sentinel-based org_id resolution (runtime-contracts §5.2, data-model §11.2)
# ---------------------------------------------------------------------------
#
# Repository methods accept an ``org_id`` keyword-only argument that defaults
# to ``AUTO_ORG``. The three possible values drive distinct behaviours,
# mirroring the ``user_id`` sentinel in ``deerflow.runtime.user_context`` so
# org-scoped reads/writes compose with the existing user filter:
#
# - :data:`AUTO_ORG` (default): read ``org_id`` from the bound
#   :class:`TenantContext`; raise :class:`RuntimeError` if no tenant is bound
#   (fail-closed, §5.2 rule 6 / §11.2 — never synthesize a default Org).
# - Explicit ``str``: use the provided id verbatim, overriding the bound
#   tenant. Useful for tests and admin-override flows.
# - Explicit ``None``: no ``org_id`` clause — the repository should skip the
#   org WHERE clause / stamp NULL. Reserved for migration scripts, the backfill
#   job and system-admin scans that intentionally bypass tenant isolation.


class _OrgIdSentinel:
    """Singleton marker meaning 'resolve org_id from the bound tenant context'."""

    _instance: _OrgIdSentinel | None = None

    def __new__(cls) -> _OrgIdSentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<AUTO_ORG>"


AUTO_ORG: Final[_OrgIdSentinel] = _OrgIdSentinel()


def resolve_org_id(
    value: str | None | _OrgIdSentinel,
    *,
    method_name: str = "repository method",
) -> str | None:
    """Resolve the ``org_id`` parameter passed to a repository method.

    Three-state semantics (mirrors
    :func:`deerflow.runtime.user_context.resolve_user_id`):

    - :data:`AUTO_ORG` (default): read ``org_id`` from the bound
      :class:`TenantContext` contextvar; raise :class:`RuntimeError` if no
      tenant is bound. This is the common case for request-scoped calls.
    - Explicit ``str``: use the provided id verbatim, overriding any bound
      tenant. Useful for tests and admin-override flows.
    - Explicit ``None``: no filter — the repository should skip the org_id
      WHERE clause / write NULL. Reserved for migration scripts, the backfill
      job and system-admin scans that intentionally bypass isolation.
    """
    if isinstance(value, _OrgIdSentinel):
        context = _current_tenant.get()
        if context is None:
            raise RuntimeError(
                f"{method_name} called with org_id=AUTO_ORG but no tenant context is bound; bind a TenantContext via bind_tenant_context at the trusted entry point, or opt out with org_id=None for migration/CLI/system-admin paths."
            )
        return context.org_id
    return value
