"""Post-restore verification gates (PR-065).

Implements the recovery-validation checks from ``capacity-and-dr.md`` §15
and ``production-runbook.md`` §10.5. Each gate is one of three statuses:

* ``PASS`` — checked and holds.
* ``FAIL`` — checked and does not hold (the restore is unsafe to open to
  traffic).
* ``SKIP`` — the code path / infrastructure the gate depends on does not
  exist today. SKIP is **not** a pass — it is an explicit, labelled
  deferral so an operator running a drill sees exactly which guarantees a
  restore did NOT get, rather than a falsely-green report. Each SKIP names
  the Track/PR it is blocked on (mirroring doctor's DEFERRED_LIVE_CHECKS).

Gates that are reachable today (data-only, no Track E / G / object-store
infrastructure):

1. **schema_compatible** — restored DB alembic head == manifest.schema_version.
2. **row_counts_match** — every manifest table's live row count == recorded.
3. **content_digests_match** — recompute each table's content digest from the
   restored rows and compare to the manifest (byte-faithful restore).
4. **no_null_org_id** — the four run-lifecycle resource tables have no
   ``org_id IS NULL`` rows (PR-024/025A invariant — a restore must not
   regress it).
5. **audit_org_isolation** — ``list_audit_events(org_a)`` returns no org_b
   rows (ADR-0005 §8 / §12.1 — org isolation survives the restore).
6. **audit_roundtrip** — insert + read back one AuditEvent (audit write +
   query are usable post-restore).

SKIP gates (infrastructure-blocked, precisely labelled):

* release_channel_points_at_valid_version / agent_digest_matches /
  new_run_pinned_to_release_ref / legacy_unpinned_count_zero (Track E)
* secret_references_resolve (Secret Store provider)
* redis_lease_reconciled (Track G PR-071/072)
* deletion_ledger_replayed (data-governance §9 — table does not exist)
* audit_archive_watermark_consistent (object storage + §10.2 archive job)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.audit.repository import insert_audit_event, list_audit_events
from deerflow.persistence.backup.manifest import BackupManifest
from deerflow.persistence.backup.snapshot import (
    _content_digest_for_rows,
    _read_alembic_head,
    _snapshot_table,
    _table_column_keys,
)
from deerflow.persistence.base import Base

logger = logging.getLogger(__name__)

#: The run-lifecycle resource tables whose ``org_id`` MUST be non-null
#: (PR-024/PR-025A NOT-NULL migration). A restore must not regress this.
_ORG_SCOPED_RESOURCE_TABLES = ("threads_meta", "runs", "run_events", "feedback")

#: The SKIP gates that are explicitly deferred to a Track/infra dependency.
#: Kept as data so the report lists every one (an operator running a drill
#: sees the full picture, not just the gates that happened to run).
_DEFERRED_GATES: tuple[tuple[str, str, str], ...] = (
    (
        "release_channel_points_at_valid_version",
        "ReleaseChannel → AgentVersion reference validity",
        "Blocked on Track E (PR-053/054): ReleaseChannel/AgentVersion tables do not exist yet.",
    ),
    (
        "agent_digest_matches",
        "Agent object digest matches manifest",
        "Blocked on Track E (PR-050/052): AgentPackage/AgentVersion tables do not exist yet.",
    ),
    (
        "new_run_pinned_to_release_ref",
        "New Run is pinned to a published ReleaseRef",
        "Blocked on Track E (PR-054/056): ReleaseRef enforcement is not implemented.",
    ),
    (
        "legacy_unpinned_count_zero",
        "legacy_unpinned Run count is 0",
        "Blocked on Track E (PR-056): legacy_run gate not implemented.",
    ),
    (
        "secret_references_resolve",
        "Connector Secret references resolve without logging values",
        "Blocked on Secret Store provider PR: only env_dev_only + reference parsing exist.",
    ),
    (
        "redis_lease_reconciled",
        "Redis lease cleaned and Run ownership reconciled",
        "Blocked on Track G (PR-071/072): no Redis lease/ownership code path exists.",
    ),
    (
        "deletion_ledger_replayed",
        "Deletion ledger / tombstone replayed after restore",
        "Blocked on data-governance §9: deletion_ledger table does not exist.",
    ),
    (
        "audit_archive_watermark_consistent",
        "Audit archive watermark matches object-summary sample",
        "Blocked on object_storage + ADR-0005 §10.2: archive job does not exist.",
    ),
)

GateStatus = Literal["PASS", "FAIL", "SKIP"]


class VerifyGateResult(BaseModel):
    name: str
    description: str
    status: GateStatus
    detail: str = ""

    model_config = {"extra": "forbid"}


class VerifyReport(BaseModel):
    gates: list[VerifyGateResult]
    passed: int
    failed: int
    skipped: int

    model_config = {"extra": "forbid"}

    @property
    def ok(self) -> bool:
        """True iff no gate FAILED (SKIP is a known deferral, not a failure)."""
        return self.failed == 0


def _gate(name: str, description: str, status: GateStatus, detail: str = "") -> VerifyGateResult:
    return VerifyGateResult(name=name, description=description, status=status, detail=detail)


async def _check_schema_compatible(session: AsyncSession, manifest: BackupManifest) -> VerifyGateResult:
    head = await _read_alembic_head(session)
    if head == manifest.schema_version and head != "unknown":
        return _gate(
            "schema_compatible",
            "Restored DB alembic head matches manifest schema_version",
            "PASS",
            f"head={head}",
        )
    return _gate(
        "schema_compatible",
        "Restored DB alembic head matches manifest schema_version",
        "FAIL",
        f"restored head={head!r} != manifest schema_version={manifest.schema_version!r}",
    )


async def _check_row_counts(session: AsyncSession, manifest: BackupManifest) -> VerifyGateResult:
    mismatches: list[str] = []
    for entry in manifest.tables:
        table = Base.metadata.tables.get(entry.name)
        if table is None:
            # Table vanished between snapshot and verify (schema drift); the
            # schema gate already FAILed, but surface it here too.
            mismatches.append(f"{entry.name}: table missing from metadata")
            continue
        live = int((await session.execute(select(func.count()).select_from(table))).scalar_one())
        if live != entry.row_count:
            mismatches.append(f"{entry.name}: {live} != {entry.row_count}")
    if mismatches:
        return _gate(
            "row_counts_match",
            "Every manifest table's live row count matches the snapshot",
            "FAIL",
            "; ".join(mismatches),
        )
    return _gate("row_counts_match", "Every manifest table's live row count matches the snapshot", "PASS")


async def _check_content_digests(session: AsyncSession, manifest: BackupManifest) -> VerifyGateResult:
    """Recompute each table's digest from restored rows; compare to manifest.

    This is the byte-faithful check: re-running the snapshot's normalisation
    over the restored rows must reproduce the recorded digest. A mismatch
    means the restore altered row contents (truncation, type coercion,
    tampering) even if the row counts matched.
    """
    mismatches: list[str] = []
    for entry in manifest.tables:
        table = Base.metadata.tables.get(entry.name)
        if table is None:
            continue
        column_keys = _table_column_keys(entry.name)
        pk_cols = tuple(c.key for c in table.primary_key.columns)
        rows, _ = await _snapshot_table(session, entry.name, column_keys, pk_fallback_order=pk_cols or None)
        digest = _content_digest_for_rows(rows)
        if digest != entry.content_digest:
            mismatches.append(entry.name)
    if mismatches:
        return _gate(
            "content_digests_match",
            "Recomputed per-table content digests match the manifest (byte-faithful restore)",
            "FAIL",
            f"digest mismatch on: {', '.join(mismatches)}",
        )
    return _gate(
        "content_digests_match",
        "Recomputed per-table content digests match the manifest (byte-faithful restore)",
        "PASS",
    )


async def _check_no_null_org_id(session: AsyncSession) -> VerifyGateResult:
    bad: list[str] = []
    for table_name in _ORG_SCOPED_RESOURCE_TABLES:
        table = Base.metadata.tables.get(table_name)
        if table is None or "org_id" not in table.c:
            continue
        null_count = int((await session.execute(select(func.count()).select_from(table).where(table.c.org_id.is_(None)))).scalar_one())
        if null_count > 0:
            bad.append(f"{table_name}={null_count}")
    if bad:
        return _gate(
            "no_null_org_id",
            "Run-lifecycle resource tables have no NULL org_id rows (PR-024/025A invariant)",
            "FAIL",
            f"NULL org_id rows: {', '.join(bad)}",
        )
    return _gate(
        "no_null_org_id",
        "Run-lifecycle resource tables have no NULL org_id rows (PR-024/025A invariant)",
        "PASS",
    )


async def _check_audit_org_isolation(sf: async_sessionmaker) -> VerifyGateResult:
    """Insert two org-distinct audit events; confirm list_audit_events is scoped.

    ``insert_audit_event`` commits its own transaction, and
    ``list_audit_events`` reads with org_id forced (ADR §8/§12.1). Verify
    neither event leaks across the org boundary on read-back.
    """
    try:
        from deerflow.contracts.events import AuditEvent, PrincipalRef

        for org in ("verify-org-a", "verify-org-b"):
            event = AuditEvent(
                event_id=f"verify-org-iso-{org}",
                idempotency_key=f"verify-org-iso-{org}",
                request_id="backup-verify",
                org_id=org,
                actor=PrincipalRef(type="user", id="verify-user"),
                action="audit.verify.org_isolation",
                outcome="success",
                occurred_at=datetime.now(UTC),
            )
            await insert_audit_event(sf, event, producer="backup-verify")
        # list_audit_events forces org_id; verify neither leaks across.
        rows_a = await list_audit_events(sf, org_id="verify-org-a", limit=10)
        leaked = [r for r in rows_a if r.org_id != "verify-org-a"]
        if leaked:
            return _gate(
                "audit_org_isolation",
                "list_audit_events(org_a) returns no org_b rows (ADR §8/§12.1)",
                "FAIL",
                f"{len(leaked)} cross-org rows leaked",
            )
        return _gate(
            "audit_org_isolation",
            "list_audit_events(org_a) returns no org_b rows (ADR §8/§12.1)",
            "PASS",
        )
    except Exception as exc:  # noqa: BLE001
        return _gate(
            "audit_org_isolation",
            "list_audit_events(org_a) returns no org_b rows (ADR §8/§12.1)",
            "FAIL",
            f"could not exercise audit org isolation: {type(exc).__name__}",
        )


async def _check_audit_roundtrip(sf: async_sessionmaker) -> VerifyGateResult:
    """One AuditEvent insert + read proves audit write+query usable post-restore."""
    try:
        from deerflow.contracts.events import AuditEvent, PrincipalRef

        event = AuditEvent(
            event_id="verify-audit-roundtrip",
            idempotency_key="verify-audit-roundtrip",
            request_id="backup-verify",
            org_id="verify-org-rt",
            actor=PrincipalRef(type="user", id="verify-user"),
            action="audit.verify.roundtrip",
            outcome="success",
            occurred_at=datetime.now(UTC),
        )
        await insert_audit_event(sf, event, producer="backup-verify")
        rows = await list_audit_events(sf, org_id="verify-org-rt", limit=10)
        if not any(r.event_id == "verify-audit-roundtrip" for r in rows):
            return _gate(
                "audit_roundtrip",
                "Audit write + query round-trip is usable post-restore",
                "FAIL",
                "inserted event not readable back",
            )
        return _gate("audit_roundtrip", "Audit write + query round-trip is usable post-restore", "PASS")
    except Exception as exc:  # noqa: BLE001
        return _gate(
            "audit_roundtrip",
            "Audit write + query round-trip is usable post-restore",
            "FAIL",
            f"could not exercise audit round-trip: {type(exc).__name__}",
        )


async def _run_gate(coro_factory: Any, name: str, gates: list[VerifyGateResult]) -> None:
    """Await a gate coroutine; on any exception, contain it into a FAIL."""
    try:
        gates.append(await coro_factory())
    except Exception as exc:  # noqa: BLE001
        gates.append(
            _gate(
                name,
                "(gate crashed)",
                "FAIL",
                f"{type(exc).__name__}: see logs",
            )
        )


async def verify_restore(sf: async_sessionmaker, manifest: BackupManifest) -> VerifyReport:
    """Run every reachable gate + list the deferred ones; return a report.

    Never raises: each gate contains its own failures into a FAIL result so
    a verify run always produces a complete report (a drill should not abort
    partway because one check blew up).

    Read-only gates (schema / row counts / digests / null-org) share one
    session. The audit gates insert+commit (``insert_audit_event`` owns its
    transaction), so each gets a fresh session via the factory — they must
    not run inside the read-only session's transaction.
    """
    import deerflow.persistence.models  # noqa: F401

    gates: list[VerifyGateResult] = []

    async with sf() as session:
        await _run_gate(lambda: _check_schema_compatible(session, manifest), "schema_compatible", gates)
        await _run_gate(lambda: _check_row_counts(session, manifest), "row_counts_match", gates)
        await _run_gate(lambda: _check_content_digests(session, manifest), "content_digests_match", gates)
        await _run_gate(lambda: _check_no_null_org_id(session), "no_null_org_id", gates)

    await _run_gate(lambda: _check_audit_org_isolation(sf), "audit_org_isolation", gates)
    await _run_gate(lambda: _check_audit_roundtrip(sf), "audit_roundtrip", gates)

    for name, description, remediation in _DEFERRED_GATES:
        gates.append(_gate(name, description, "SKIP", remediation))

    passed = sum(1 for g in gates if g.status == "PASS")
    failed = sum(1 for g in gates if g.status == "FAIL")
    skipped = sum(1 for g in gates if g.status == "SKIP")
    return VerifyReport(gates=gates, passed=passed, failed=failed, skipped=skipped)


__all__ = [
    "GateStatus",
    "VerifyGateResult",
    "VerifyReport",
    "verify_restore",
]
