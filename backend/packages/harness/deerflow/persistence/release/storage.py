"""Object storage abstraction for agent artifacts (PR-052).

ADR-0004 §11.1/§11.2: small artifacts may live inline in PostgreSQL; large
artifacts are addressed by an object key. The storage backend is an
abstraction so the repository can route by the production threshold without
coupling to a specific provider.

This module ships:

* ``ObjectStore`` — the harness-facing Protocol (``put`` / ``get`` /
  ``exists`` / ``delete`` / ``compute_object_key``). Real S3/MinIO
  implementations land in a follow-up PR (they need ``aiobotocore``,
  credentials, and a doctor probe for the private/encrypted guarantees of
  ADR §11.2).
* ``InlineObjectStore`` — the MVP default backend. It is a **no-op**: the
  artifact bytes already live in ``AgentVersionRow.content_inline`` (the
  repository routes small payloads there), so ``put`` records the key but
  stores nothing externally, ``get`` returns empty bytes, and ``exists`` is
  always True. This keeps the repository's storage-routing code path uniform
  across inline and object backends while requiring zero external
  infrastructure — exactly the "abstraction first, real backend later"
  pattern used by OIDC (ADR-0003 §10) and the SecretStore config.

The key shape follows ADR §11.2::

    org/{org_id}/workspace/{workspace_id-or-_default}/agent-version/{version_id}/artifact

The default workspace sentinel is ``_default`` so a NULL workspace still
produces a stable, collision-free key prefix.
"""

from __future__ import annotations

from typing import Protocol

#: Sentinel workspace segment for versions without a workspace_id (ADR §11.2
#: ``workspace_id-or-_default``). Kept stable so keys are collision-free.
DEFAULT_WORKSPACE_SEGMENT = "_default"


def compute_object_key(*, org_id: str, workspace_id: str | None, version_id: str) -> str:
    """Return the canonical object key for a version's artifact (ADR §11.2).

    The key is Org-scoped and includes the version id, so cross-Org and
    cross-version collisions are structurally impossible. ``workspace_id``
    is optional; a missing workspace uses the ``_default`` sentinel.
    """
    ws = workspace_id if workspace_id else DEFAULT_WORKSPACE_SEGMENT
    return f"org/{org_id}/workspace/{ws}/agent-version/{version_id}/artifact"


class ObjectStore(Protocol):
    """Storage backend for large agent artifacts (ADR §11.2).

    The repository calls ``put`` after computing the digest and before the
    row is committed (ADR §11.2 "上传完成后再使 Version 可用"). Implementations
    are best-effort durable: a ``put`` that raises aborts the caller's
    transaction. ``get`` / ``exists`` / ``delete`` are addressed by the
    opaque key returned from ``put``.
    """

    def put(self, *, object_key: str, content: bytes) -> None:
        """Store ``content`` under ``object_key``; raise on failure."""
        ...

    def get(self, object_key: str) -> bytes:
        """Return the bytes stored under ``object_key``; raise if missing."""
        ...

    def exists(self, object_key: str) -> bool:
        """Return whether ``object_key`` has a stored object."""
        ...

    def delete(self, object_key: str) -> None:
        """Remove the object under ``object_key``; idempotent."""
        ...


class InlineObjectStore:
    """MVP backend: artifact bytes live in ``content_inline``, not here.

    The repository routes small payloads to the inline column directly, so
    this store stores nothing externally. It exists so the repository's
    object-routing branch has a uniform call site (``store.put`` /
    ``store.exists``) even when the chosen backend is inline — the same code
    path a future S3 backend will exercise.

    ``put`` validates the key shape and records it (for log/diagnostic
    parity) but stores no bytes. ``get`` returns empty bytes (the inline
    column is the source of truth). ``exists`` is always ``True`` because an
    inline artifact is never "missing" from its own row. ``delete`` is a
    no-op (the row owns the content; GC is a separate concern, ADR §11.3).
    """

    def put(self, *, object_key: str, content: bytes) -> None:
        # No external storage — content_inline is the source of truth. We
        # accept the call so the repository routing is backend-agnostic.
        _ = (object_key, content)  # diagnostic-only; nothing to persist

    def get(self, object_key: str) -> bytes:
        # The inline column holds the real bytes; this store has none. A
        # caller reading artifact content MUST read content_inline, not this.
        _ = object_key
        return b""

    def exists(self, object_key: str) -> bool:
        # An inline artifact is always present when its row is present.
        _ = object_key
        return True

    def delete(self, object_key: str) -> None:
        # Row-owned content; deletion is the row's responsibility (ADR §11.3).
        _ = object_key
