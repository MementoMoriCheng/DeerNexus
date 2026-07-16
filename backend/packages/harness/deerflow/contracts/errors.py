"""Stable error model for runtime contracts.

Freezes the cross-process error envelope and the MVP error code registry
defined in ``docs/architecture/runtime-contracts.md`` §12.

The envelope's ``code`` is typed as a free ``str`` so an unknown code (for
example a newer producer adding a code an older consumer does not know yet)
still deserializes safely; consumers then decide how to handle it per §3
("unknown enum values must be safely rejected or ignored, never silently
remapped"). :class:`ErrorCode` is the registry of known codes for producers
and consumers to reference.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(StrEnum):
    """MVP stable error codes (runtime-contracts.md §12).

    Adding a code is a compatible change as long as old consumers can safely
    reject the unknown value. Do not change an existing code's string value.
    """

    TENANT_CONTEXT_MISSING = "tenant_context_missing"
    TENANT_MISMATCH = "tenant_mismatch"
    AUTHENTICATION_INVALID = "authentication_invalid"
    PRINCIPAL_DISABLED = "principal_disabled"
    ORG_SUSPENDED = "org_suspended"
    ORG_DELETING = "org_deleting"
    PERMISSION_DENIED = "permission_denied"
    POLICY_DENIED = "policy_denied"
    POLICY_UNAVAILABLE = "policy_unavailable"
    APPROVAL_REQUIRED = "approval_required"
    RELEASE_NOT_FOUND = "release_not_found"
    RELEASE_NOT_PUBLISHED = "release_not_published"
    RELEASE_REVOKED = "release_revoked"
    RELEASE_UNPINNED = "release_unpinned"
    RELEASE_TENANT_MISMATCH = "release_tenant_mismatch"
    RELEASE_CONFLICT = "release_conflict"
    RUN_CONFLICT = "run_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    AUDIT_UNAVAILABLE = "audit_unavailable"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"


_RETRYABLE_CODES = frozenset(
    {
        ErrorCode.POLICY_UNAVAILABLE,
        ErrorCode.RELEASE_CONFLICT,
        ErrorCode.RUN_CONFLICT,
        ErrorCode.AUDIT_UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
    }
)


def is_retryable_code(code: ErrorCode | str) -> bool:
    """Return whether an error code is retryable per the §12 table.

    ``policy_unavailable`` is marked retryable at the contract level; high-risk
    callers still fail closed on evaluation unavailability (see §7.4).
    """
    return code in _RETRYABLE_CODES


class ContractError(BaseModel):
    """Stable error envelope shared across all runtime boundaries.

    The envelope never carries secrets, stack traces, SQL, internal paths or
    full policy text. Diagnostic detail may live in ``details`` but must follow
    the same no-secret rule and an allow-list schema.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    code: str = Field(
        description="Stable error code; conventionally an ErrorCode value.",
    )
    message: str = Field(
        description="Safe, non-sensitive human-readable summary.",
    )
    retryable: bool = Field(
        description="Whether the caller may back off and retry.",
    )
    request_id: str = Field(
        min_length=1,
        description="Correlation id of the originating request; never empty.",
    )
    details: dict = Field(
        default_factory=dict,
        description="Optional diagnostic payload; no secrets, tokens or prompts.",
    )

    @classmethod
    def from_code(
        cls,
        code: ErrorCode | str,
        *,
        request_id: str,
        message: str = "",
        details: dict | None = None,
    ) -> Self:
        """Build an envelope from a known code, deriving ``retryable``.

        Centralizes the retryable mapping so producers cannot accidentally mark
        a non-retryable security failure as retryable.
        """
        return cls(
            code=str(code),
            message=message or str(code),
            retryable=is_retryable_code(code),
            request_id=request_id,
            details=details or {},
        )
