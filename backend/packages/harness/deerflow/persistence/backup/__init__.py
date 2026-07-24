"""Application-level backup / restore evidence layer (PR-065).

This package implements the DeerNexus backup Job's evidence half
(pr-split-guide §11 / runbook §9 / §17). It is **not** a physical DB dump:
it snapshots every DeerFlow-owned table into a backend-neutral, digest-stamped
manifest + content files, and provides a restore-into-empty-DB path plus
post-restore verification gates (capacity-and-dr §15 / runbook §10.5).

Modules:
* :mod:`backup.manifest` — ``BackupManifest`` model + read/write + tamper
  evidence (sha256 over manifest body with the digest fields blanked).
* :mod:`backup.snapshot` — read every ``Base.metadata`` table via Core,
  normalise rows, stamp per-table content digests.
* :mod:`backup.restore` — reload a snapshot into an empty DB (DR scenario).
* :mod:`backup.verify` — recovery gates (PASS/FAIL/SKIP), with infra-blocked
  gates precisely labelled by Track/PR.

Scope honesty: this is the **application's** evidence layer. The operator's
DB platform owns pg_dump/WAL/PITR (runbook §9.1); object-storage artifact +
audit-archive snapshots, Secret Store metadata, and deletion-ledger replay
are deferred to their respective infra PRs. The doctor probe
(``app/doctor/probes/backup_probe.py``) surfaces a manifest's freshness but
its PASS message explicitly states it complements — not replaces — the DB
platform backup.
"""

from deerflow.persistence.backup.manifest import (
    MANIFEST_FILENAME,
    BackupManifest,
    BackupTableEntry,
    compute_content_digest,
    compute_manifest_digest,
    finalize_digests,
    latest_manifest,
    load_manifest,
    verify_manifest_integrity,
    write_manifest,
)
from deerflow.persistence.backup.restore import (
    SNAPSHOT_CONTENT_DIR,
    RestoreError,
    RestoreReport,
    restore_from_manifest,
)
from deerflow.persistence.backup.snapshot import (
    read_table_rows,
    take_snapshot,
    write_table_rows,
)
from deerflow.persistence.backup.verify import (
    GateStatus,
    VerifyGateResult,
    VerifyReport,
    verify_restore,
)

__all__ = [
    "MANIFEST_FILENAME",
    "BackupManifest",
    "BackupTableEntry",
    "GateStatus",
    "RestoreError",
    "RestoreReport",
    "SNAPSHOT_CONTENT_DIR",
    "VerifyGateResult",
    "VerifyReport",
    "compute_content_digest",
    "compute_manifest_digest",
    "finalize_digests",
    "latest_manifest",
    "load_manifest",
    "read_table_rows",
    "restore_from_manifest",
    "take_snapshot",
    "verify_manifest_integrity",
    "verify_restore",
    "write_manifest",
    "write_table_rows",
]
