"""Artifact storage reconciliation (PR-052).

ADR-0004 §11.2: "Object 与数据库定期对账;缺失或 digest 不匹配时拒绝
执行并告警" (reconcile object storage against the DB periodically; on a
missing or digest-mismatched object, refuse execution and alert).

This module provides the read-only reconciliation: it walks the
``object_key``-bearing ``AgentVersionRow`` set in an Org and checks each
against the ``ObjectStore``. Inline-only artifacts (``content_inline`` set,
``object_key`` NULL) are always present (the row is the source of truth) and
are skipped — they cannot be "missing" from their own row.

A real S3 backend (follow-up) makes this meaningful: a deleted/corrupted
object would surface as a ``missing_versions`` entry. With the MVP
``InlineObjectStore`` (``exists`` always True) reconciliation reports zero
missing — the value today is the *interface* and the diagnostic report a
future S3 backend + a doctor probe will consume. It does NOT auto-alert to
metrics in this PR (that wiring is a follow-up, like the other
doctor-promote steps); it returns a structured report the router surfaces
read-only to the Org admin.

Note on digest mismatch (ADR §11.2 "digest 不匹配"): verifying that the
stored object's bytes hash back to ``row.digest`` requires reading the full
object on every reconcile. That is expensive at scale and is gated behind a
``verify_digest`` flag (default False) — a production reconcile job flips it
on. With the inline backend the bytes live in the row, so the check is
exact and cheap; with S3 it streams the object.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.release.digest import compute_artifact_digest
from deerflow.persistence.release.model import AgentVersionRow
from deerflow.persistence.release.storage import InlineObjectStore, ObjectStore


@dataclass(frozen=True)
class MissingVersion:
    """A version whose object is absent or whose digest no longer matches.

    ``reason`` is ``"missing_object"`` (``store.exists`` False) or
    ``"digest_mismatch"`` (the re-read bytes hash to a different digest than
    the row pinned). Either case means the immutable artifact identity is
    broken and the version must NOT be executed (ADR §11.2).
    """

    version_id: str
    package_id: str
    object_key: str
    reason: str


@dataclass(frozen=True)
class ReconcileReport:
    """Result of a single reconciliation pass over an Org's object-backed versions.

    ``checked_count`` is the number of object-backed versions inspected
    (inline-only versions are excluded — they cannot be missing from their
    own row). ``missing_versions`` lists every broken reference. An empty
    ``missing_versions`` with ``checked_count > 0`` means all object-backed
    artifacts are present and (if ``verify_digest`` was set) digest-valid.
    """

    org_id: str
    checked_count: int
    missing_versions: list[MissingVersion] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True when no broken references were found."""
        return not self.missing_versions


async def reconcile_versions(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    object_store: ObjectStore | None = None,
    verify_digest: bool = False,
) -> ReconcileReport:
    """Reconcile object-backed AgentVersions in ``org_id`` against the store.

    Walks every version with a non-null ``object_key`` and verifies the
    object exists (always) and, when ``verify_digest`` is set, that its
    re-read bytes hash back to the pinned ``digest`` (ADR §11.2). Inline
    versions (``object_key`` NULL) are structurally always-present and are
    skipped — they have no external object to lose.

    Returns a :class:`ReconcileReport`; never raises on a missing/mismatched
    object (that is the report's content). A ``store.get`` that raises on a
    corrupt object is treated as a digest mismatch so a broken blob does not
    abort the whole pass.
    """
    if not org_id:
        raise ValueError("org_id is required for reconciliation")
    store = object_store if object_store is not None else InlineObjectStore()

    async with sf() as session:
        stmt = select(AgentVersionRow).where(
            AgentVersionRow.org_id == org_id,
            AgentVersionRow.object_key.is_not(None),
        )
        rows = list((await session.execute(stmt)).scalars().all())

    missing: list[MissingVersion] = []
    for row in rows:
        assert row.object_key is not None  # narrowed by the query
        if not store.exists(row.object_key):
            missing.append(MissingVersion(row.id, row.package_id, row.object_key, "missing_object"))
            continue
        if verify_digest:
            try:
                stored = store.get(row.object_key)
            except Exception:  # noqa: BLE001 — a corrupt read is a mismatch, not an abort
                missing.append(MissingVersion(row.id, row.package_id, row.object_key, "digest_mismatch"))
                continue
            if compute_artifact_digest(stored) != row.digest:
                missing.append(MissingVersion(row.id, row.package_id, row.object_key, "digest_mismatch"))

    return ReconcileReport(org_id=org_id, checked_count=len(rows), missing_versions=missing)
