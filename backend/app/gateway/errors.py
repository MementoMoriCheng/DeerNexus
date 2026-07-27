"""Shared HTTP-bound error envelopes for gateway routers.

Centralizes the ``ContractError`` → ``HTTPException`` translation so every
router emits the same ``{code, message, retryable, request_id, details}``
shape (ADR-0005 §12 / errors.md). ``agent_artifacts.py`` originated the two
helpers; they were extracted here so the runs/release routers can reuse them
without an import cycle (``routers.agent_artifacts`` imports a lot of
release-domain modules that the runs router does not need).
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from starlette import status

from deerflow.contracts.errors import ContractError, ErrorCode


def request_id(request: Request) -> str:
    """Correlation id of the originating request (never empty).

    CorrelationMiddleware (outermost) sets ``request.state.request_id``; this
    is the same id the tenant/audit stack uses, so error envelopes share it.
    """
    return str(getattr(request.state, "request_id", "") or "unknown")


def contract_error_response(
    request: Request,
    code: ErrorCode,
    *,
    status_code: int,
    message: str = "",
    details: dict | None = None,
) -> HTTPException:
    """Build a uniform ``ContractError`` envelope as an ``HTTPException``.

    The detail is the serialized ``ContractError`` dict, so a caller sees the
    same ``{code, message, retryable, request_id, details}`` shape across every
    failure. ``retryable`` is derived from the code by ``ContractError.from_code``.
    """
    err = ContractError.from_code(
        code,
        request_id=request_id(request),
        message=message,
        details=details or {},
    )
    return HTTPException(status_code=status_code, detail=err.model_dump())


#: HTTP status code per ``ReleaseResolutionError.code`` (resolver docstring
#: §"Failure semantics"; runtime-contracts §12). The resolver raises codes that
#: align 1:1 with ``ErrorCode`` members; this maps each to its HTTP boundary.
#: ``release_unpinned`` is intentionally absent — that code is raised by the
#: legacy resume gate, not the resolver (the resolver never sees a pinned run).
_RELEASE_RESOLUTION_STATUS: dict[str, int] = {
    "release_not_found": status.HTTP_404_NOT_FOUND,
    "release_not_published": status.HTTP_409_CONFLICT,
    "release_revoked": status.HTTP_409_CONFLICT,
    "release_tenant_mismatch": status.HTTP_403_FORBIDDEN,
}


def release_resolution_error_response(request: Request, exc: Exception) -> HTTPException:
    """Translate a ``ReleaseResolutionError`` (from the resolver) into an HTTP envelope.

    The exception carries a ``code`` attribute (one of the
    ``RELEASE_RESOLUTION_STATUS`` keys). Unknown codes fall back to 409 +
    ``release_not_found`` (existence-hiding, the resolver's default posture for
    anything it cannot classify) so a future code added to the resolver never
    leaks a 500.
    """
    code = str(getattr(exc, "code", "") or "release_not_found")
    status_code = _RELEASE_RESOLUTION_STATUS.get(code, status.HTTP_409_CONFLICT)
    return contract_error_response(
        request,
        ErrorCode(code) if code in ErrorCode._value2member_map_ else ErrorCode.RELEASE_NOT_FOUND,
        status_code=status_code,
        message=str(exc),
    )
