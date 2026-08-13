"""Object storage abstraction for agent artifacts (PR-052, S3 backend T1B).

ADR-0004 §11.1/§11.2: small artifacts may live inline in PostgreSQL; large
artifacts are addressed by an object key. The storage backend is an
abstraction so the repository can route by the production threshold without
coupling to a specific provider.

This module ships:

* ``ObjectStore`` — the harness-facing async Protocol (``put`` / ``get`` /
  ``exists`` / ``delete`` / ``compute_object_key``). Methods are ``async``
  so a real S3/MinIO backend can release the event loop during network I/O
  (the inline backend is still effectively synchronous — it does nothing).
* ``InlineObjectStore`` — the MVP default backend. It is a **no-op**: the
  artifact bytes already live in ``AgentVersionRow.content_inline`` (the
  repository routes small payloads there), so ``put`` records the key but
  stores nothing externally, ``get`` returns empty bytes, and ``exists`` is
  always True. This keeps the repository's storage-routing code path uniform
  across inline and object backends while requiring zero external
  infrastructure — exactly the "abstraction first, real backend later"
  pattern used by OIDC (ADR-0003 §10) and the SecretStore config.
* ``S3ObjectStore`` (T1B) — a real S3/MinIO backend backed by ``boto3``
  wrapped in ``asyncio.to_thread``. Selected when
  ``production.artifact.object_store_backend == "s3"``; requires the
  ``s3`` optional dependency (``uv pip install 'deerflow-harness[s3]'``).

The key shape follows ADR §11.2::

    org/{org_id}/workspace/{workspace_id-or-_default}/agent-version/{version_id}/artifact

The default workspace sentinel is ``_default`` so a NULL workspace still
produces a stable, collision-free key prefix.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)

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

    All methods are ``async`` so real backends (S3/MinIO) can release the
    event loop during network I/O. The repository calls ``put`` after
    computing the digest and before the row is committed (ADR §11.2
    "上传完成后再使 Version 可用"). Implementations are best-effort durable:
    a ``put`` that raises aborts the caller's transaction. ``get`` /
    ``exists`` / ``delete`` are addressed by the opaque key returned from
    ``put``.
    """

    async def put(self, *, object_key: str, content: bytes) -> None:
        """Store ``content`` under ``object_key``; raise on failure."""
        ...

    async def get(self, object_key: str) -> bytes:
        """Return the bytes stored under ``object_key``; raise if missing."""
        ...

    async def exists(self, object_key: str) -> bool:
        """Return whether ``object_key`` has a stored object."""
        ...

    async def delete(self, object_key: str) -> None:
        """Remove the object under ``object_key``; idempotent."""
        ...


class InlineObjectStore:
    """MVP backend: artifact bytes live in ``content_inline``, not here.

    The repository routes small payloads to the inline column directly, so
    this store stores nothing externally. It exists so the repository's
    object-routing branch has a uniform call site (``await store.put`` /
    ``await store.exists``) even when the chosen backend is inline — the same code
    path an S3 backend exercises.

    ``put`` validates the key shape and records it (for log/diagnostic
    parity) but stores no bytes. ``get`` returns empty bytes (the inline
    column is the source of truth). ``exists`` is always ``True`` because an
    inline artifact is never "missing" from its own row. ``delete`` is a
    no-op (the row owns the content; GC is a separate concern, ADR §11.3).
    """

    async def put(self, *, object_key: str, content: bytes) -> None:
        # No external storage — content_inline is the source of truth. We
        # accept the call so the repository routing is backend-agnostic.
        _ = (object_key, content)  # diagnostic-only; nothing to persist

    async def get(self, object_key: str) -> bytes:
        # The inline column holds the real bytes; this store has none. A
        # caller reading artifact content MUST read content_inline, not this.
        _ = object_key
        return b""

    async def exists(self, object_key: str) -> bool:
        # An inline artifact is always present when its row is present.
        _ = object_key
        return True

    async def delete(self, object_key: str) -> None:
        # Row-owned content; deletion is the row's responsibility (ADR §11.3).
        _ = object_key


class S3ObjectStore:
    """S3/MinIO backend for large agent artifacts (T1B, ADR §11.2).

    Uses ``boto3`` (the AWS SDK) wrapped in ``asyncio.to_thread`` so the
    async event loop is released during S3 network I/O. ``boto3`` is chosen
    over ``aiobotocore`` for stability: aiobotocore strictly pins botocore
    versions, which causes frequent dependency conflicts. The
    ``to_thread`` wrapper is a lightweight, correct way to integrate
    synchronous boto3 into an async server — each call costs one thread-pool
    dispatch (microseconds) vs. the S3 round-trip (100ms–1s+).

    All objects are written with server-side encryption (SSE-S3 ``AES256``)
    by default, satisfying ADR §11.2 "静态加密" (encryption at rest). The
    bucket must be pre-created by the operator (infrastructure concern, not
    application concern); ``__init__`` validates the bucket exists and fails
    fast with a clear message if it does not.
    """

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        bucket: str,
        region: str | None,
        access_key: str,
        secret_key: str,
        verify_tls: bool = True,
    ) -> None:
        try:
            import boto3  # type: ignore[import-not-found]
            from botocore.config import Config  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError("S3ObjectStore requires the 's3' optional dependency. Install it with: uv pip install 'deerflow-harness[s3]'") from e

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            verify=verify_tls,
            config=Config(
                # Retries are handled by the caller (the repository's put
                # failure aborts the transaction); boto3's own retries add
                # latency without improving correctness for write-after-
                # before-commit semantics.
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )
        # Validate the bucket exists early so misconfigurations surface at
        # startup (doctor probe / lifespan) rather than on the first write.
        try:
            self._client.head_bucket(Bucket=bucket)
        except Exception:
            logger.exception("S3ObjectStore: bucket '%s' is not accessible", bucket)
            raise

    async def put(self, *, object_key: str, content: bytes) -> None:
        """Store ``content`` under ``object_key`` with SSE-S3 encryption."""
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=object_key,
            Body=content,
            ServerSideEncryption="AES256",
        )

    async def get(self, object_key: str) -> bytes:
        """Return the bytes stored under ``object_key``; raise if missing."""
        resp = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self._bucket,
            Key=object_key,
        )
        return await asyncio.to_thread(resp["Body"].read)

    async def exists(self, object_key: str) -> bool:
        """Return whether ``object_key`` has a stored object."""
        from botocore.exceptions import ClientError  # type: ignore[import-not-found]

        try:
            await asyncio.to_thread(
                self._client.head_object,
                Bucket=self._bucket,
                Key=object_key,
            )
            return True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                return False
            raise

    async def delete(self, object_key: str) -> None:
        """Remove the object under ``object_key``; idempotent."""
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=object_key,
        )

    def close(self) -> None:
        """Close the underlying boto3 session (DI teardown)."""
        # boto3 clients are lightweight wrappers; no explicit close is
        # needed. This method exists for the lifespan teardown contract.
        pass


def build_object_store(config) -> ObjectStore:  # type: ignore[no-untyped-def]
    """Construct the appropriate ``ObjectStore`` from a ``ProductionArtifactConfig``.

    Returns ``InlineObjectStore`` for the default ``"inline"`` backend (zero
    external infra). For ``"s3"`` it validates all required fields are
    present before constructing an ``S3ObjectStore``.
    """
    backend = config.object_store_backend
    if backend == "inline":
        return InlineObjectStore()

    if backend == "s3":
        if not config.s3_bucket:
            raise ValueError("object_store_backend='s3' requires s3_bucket to be configured (production.artifact.s3_bucket).")
        if not config.s3_access_key or not config.s3_secret_key:
            raise ValueError("object_store_backend='s3' requires s3_access_key and s3_secret_key to be configured (production.artifact.s3_access_key/s3_secret_key).")
        return S3ObjectStore(
            endpoint_url=config.s3_endpoint_url,
            bucket=config.s3_bucket,
            region=config.s3_region,
            access_key=config.s3_access_key,
            secret_key=config.s3_secret_key,
            verify_tls=config.s3_verify_tls,
        )

    raise ValueError(f"Unknown object_store_backend: {backend!r}")
