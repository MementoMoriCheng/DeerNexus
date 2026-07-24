"""Backup freshness probe for the production doctor (PR-065).

Implements the ``backup.freshness`` check: verifies that the application-level
backup Job (``scripts/backup.py``) has produced a manifest within the
operator's declared RPO, and that the manifest is internally consistent
(its tamper-evidence digests still recompute). This is a true live check —
it reads the manifest file the Job wrote, not just a config declaration.

Honesty contract (runbook §9.1): this probe verifies the **DeerNexus
application-level backup evidence layer**. It is a **complement to**, not a
replacement for, the DB platform's backup (pg_dump / WAL / PITR, which the
operator's managed Postgres owns). A PASS here means "the DeerNexus backup
Job ran within RPO and its manifest is intact" — it does NOT mean "the DB is
backed up." The PASS message states this explicitly so a green probe is not
misread as full-coverage backup evidence.

The probe follows the ``audit_probe`` / ``postgres_probe`` isolation contract:
it never touches the global engine, never writes, and contains any failure
into a FAIL result. It does not need a DB connection at all — the manifest
is a file — so it is safe to run even when the DB is unreachable (a backup
freshness signal is independent of live DB health).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_CHECK_ID = "backup.freshness"
_COMPONENT = "backup"
_CONFIG_SOURCE = "config.yaml:production.backup"

#: Grace over the declared RPO before the probe FAILs. The Job's own
#: scheduling jitter (cron minute, lock contention) should not flip a backup
#: to P1 the instant the RPO elapses; a small grace keeps the alert honest.
#: This is advisory — operators tuning tighter RPOs should lower it.
_RPO_GRACE_SECONDS = 300.0


def _result(status: DoctorStatus, message: str, remediation: str | None = None) -> DoctorCheckResult:
    return DoctorCheckResult(
        check_id=_CHECK_ID,
        status=status,
        component=_COMPONENT,
        message=message,
        remediation=remediation,
        config_source=_CONFIG_SOURCE,
    )


def _complement_note() -> str:
    """The honest caveat stamped on every PASS — see module docstring."""
    return "Complements (does NOT replace) your DB platform backup (pg_dump/WAL/PITR, runbook §9.1)."


async def probe_backup_freshness(config: AppConfig) -> DoctorCheckResult:
    """Verify the latest backup manifest is within RPO and tamper-intact.

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Never raises.
    """
    backup = config.production.backup

    # The application-level backup is opt-in. A deployment that has not
    # declared a backup destination is not backed up at this layer at all;
    # whether that is a hard FAIL or a WARN depends on whether the operator
    # declared backups enabled (intent: "I rely on this") vs disabled
    # (intent: "I'm handling backup at the DB platform level only").
    if not backup.enabled or not backup.destination_dir:
        return _result(
            DoctorStatus.WARN,
            "Application-level backup Job is not enabled — only a DB platform backup (if declared) protects this deployment. "
            "Set production.backup.enabled=true and production.backup.destination_dir to enable the DeerNexus evidence-layer backup.",
        )

    destination = Path(backup.destination_dir)
    try:
        from deerflow.persistence.backup import latest_manifest, verify_manifest_integrity
    except Exception:  # noqa: BLE001 — backup layer broken is a FAIL
        logger.warning("backup evidence layer not importable", exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            "Could not import deerflow.persistence.backup — the backup evidence layer is broken.",
            "Reinstall deps (uv sync) and verify the gateway imports cleanly.",
        )

    try:
        manifest = latest_manifest(destination)
    except Exception:  # noqa: BLE001 — contain any manifest-read failure
        logger.warning("backup manifest read failed", exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            f"Could not read a backup manifest from {destination} — the directory is unreadable or a manifest is corrupted.",
            f"Inspect {destination}; remove a corrupted manifest.json so the Job writes a fresh one on its next run.",
        )

    if manifest is None:
        # Enabled + destination set, but no manifest yet. This is the
        # "declared but never ran" state — a real gap if the operator is
        # relying on this layer; FAIL so it is not silently empty.
        return _result(
            DoctorStatus.FAIL,
            f"Backup is enabled (destination_dir={destination}) but no manifest exists — the Job has never run successfully.",
            "Run `python -m scripts.backup` (or the scheduled cron) to produce the first backup manifest.",
        )

    # Tamper evidence: recompute both digests and compare to the stored values.
    manifest_path = destination / "manifest.json"
    # ``latest_manifest`` may have returned a timestamped sidecar; fall back
    # to the canonical path for integrity (the manifest object carries the
    # authoritative digests either way).
    ok, _loaded = verify_manifest_integrity(manifest_path)
    if not ok:
        # A timestamped sidecar could be the latest; check it too before
        # declaring tamper, so an old canonical file does not mask a valid
        # newer sidecar.
        for child in destination.glob("manifest-*.json"):
            ok_side, _ = verify_manifest_integrity(child)
            if ok_side and latest_manifest(destination) is not None:
                ok = True
                break
    if not ok:
        return _result(
            DoctorStatus.FAIL,
            "Backup manifest failed tamper-evidence verification — its content/manifest digests no longer recompute from the stored body. The manifest or its snapshot was altered after the Job wrote it.",
            "Re-run the backup Job to produce a fresh manifest; investigate how the existing one was modified (operator error, compromised backup target).",
        )

    # Freshness within declared RPO (runbook §14.2 P1).
    created_at = manifest.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    age_seconds = max(0.0, (now - created_at).total_seconds())
    rpo_seconds = backup.declared_rpo_hours * 3600
    if age_seconds > rpo_seconds + _RPO_GRACE_SECONDS:
        return _result(
            DoctorStatus.FAIL,
            f"Latest backup manifest is {age_seconds / 3600:.1f}h old, exceeding the declared RPO of {backup.declared_rpo_hours}h (runbook §14.2 P1). " + _complement_note(),
            "Re-run the backup Job and verify its cron schedule is firing; investigate why it fell behind.",
        )

    return _result(
        DoctorStatus.PASS,
        f"Latest backup manifest is {age_seconds / 3600:.1f}h old (within declared RPO of {backup.declared_rpo_hours}h), backup_id={manifest.backup_id}, schema_version={manifest.schema_version}. " + _complement_note(),
    )


__all__ = ["probe_backup_freshness"]
