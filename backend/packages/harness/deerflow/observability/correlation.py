"""Correlation-context ContextVar and inbound request-id validation (PR-062).

Holds the per-request / per-task correlation fields from
``docs/ops/observability-and-slo.md`` §2 (request_id, trace_id, span_id,
org_id, workspace_id, principal_type, principal_id, thread_id, run_id,
release_digest, policy_version, deployment_version, environment, service).

The lifecycle helpers mirror the established pattern in
``deerflow.contracts.context`` (``bind_tenant_context`` /
``reset_tenant_context`` / ``get_tenant_context``) so the asyncio / thread /
``asyncio.create_task`` semantics already documented there carry over
unchanged:

* ``ContextVar`` is task-local under asyncio, not thread-local;
* ``asyncio.create_task`` and ``asyncio.to_thread`` inherit the parent task's
  context — this is what lets a Run's worker task see the request's
  correlation even though the request itself may have returned;
* ``bind`` must always be paired with ``reset`` in a ``try/finally``.

``validate_inbound_request_id`` enforces §2's "客户端提交的关联 ID 必须校验
长度和字符，不能造成日志注入" rule: an inbound ``X-Request-Id`` outside the
allowed alphabet / length is rejected (returns ``None``) and the caller
generates a fresh id rather than trust the client value.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Final

# Allowed alphabet + length envelope for inbound X-Request-Id values.
# RFC 4122 hex UUIDs, dotted-decimal deployment ids and short slugs all fit;
# control characters, whitespace, newlines, quotes and the JSON structural
# punctuation that would enable log injection do not.
_REQUEST_ID_MAX_LEN: Final[int] = 128
_REQUEST_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class CorrelationContext:
    """Per-request / per-task correlation fields (observability-and-slo §2).

    Frozen so the context is immutable once bound; the only way to change a
    field is to construct a new ``CorrelationContext`` and bind it. Only
    ``request_id`` is required — every other field is populated as the
    request progresses through the stack (org_id / principal_… after tenant
    resolution, thread_id / run_id once a run is bound, release_digest /
    policy_version from the release / admission paths).
    """

    request_id: str
    trace_id: str | None = None
    span_id: str | None = None
    org_id: str | None = None
    workspace_id: str | None = None
    principal_type: str | None = None
    principal_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None
    release_digest: str | None = None
    policy_version: str | None = None
    deployment_version: str | None = None
    environment: str | None = None
    service: str | None = None


# ---------------------------------------------------------------------------
# ContextVar lifecycle (mirror contracts/context.py:116-178)
# ---------------------------------------------------------------------------

_current_correlation: Final[ContextVar[CorrelationContext | None]] = ContextVar(
    "deerflow_current_correlation",
    default=None,
)


def bind_correlation(context: CorrelationContext) -> Token[CorrelationContext | None]:
    """Bind ``context`` for the current async task / thread.

    Returns a reset token that should be passed to :func:`reset_correlation`
    in a ``finally`` block to restore the previous context. Use ``try/finally``
    so the contextvar is restored on both normal and exceptional exits —
    failing to reset leaks the correlation across task reuse.
    """
    return _current_correlation.set(context)


def reset_correlation(token: Token[CorrelationContext | None]) -> None:
    """Restore the correlation context to the state captured by ``token``."""
    _current_correlation.reset(token)


def get_correlation() -> CorrelationContext | None:
    """Return the current correlation context, or ``None`` if unset.

    Safe to call in any context (request task, background task, sync thread).
    Loggers / formatters / span helpers call this to enrich their output;
    absence is non-fatal — they simply omit the correlation fields.
    """
    return _current_correlation.get()


# ---------------------------------------------------------------------------
# Inbound request id validation (§2 "校验长度和字符，不能造成日志注入")
# ---------------------------------------------------------------------------


def new_request_id() -> str:
    """Generate a fresh per-request correlation id (RFC 4122 hex UUID)."""
    return uuid.uuid4().hex


def validate_inbound_request_id(raw: str | None) -> str | None:
    """Validate an inbound ``X-Request-Id`` header value (§2).

    Returns the trimmed value when it is well-formed (length 1–128, only
    ``[A-Za-z0-9._-]``), else ``None``. ``None`` tells the caller to
    generate a fresh id rather than trust the client value — this is the
    anti-log-injection gate mandated by §2.
    """
    if raw is None:
        return None
    trimmed = raw.strip()
    if not trimmed or len(trimmed) > _REQUEST_ID_MAX_LEN:
        return None
    if not _REQUEST_ID_PATTERN.match(trimmed):
        return None
    return trimmed


__all__ = [
    "CorrelationContext",
    "bind_correlation",
    "get_correlation",
    "new_request_id",
    "reset_correlation",
    "validate_inbound_request_id",
]
