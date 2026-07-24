"""Backup manifest model + read/write/integrity helpers (PR-065).

The manifest is the **evidence layer** of DeerNexus's application-level
backup Job (pr-split-guide §11 / runbook §9 / §17). It is **not** a physical
DB dump: it is a portable, tamper-evident description of what the Job
snapshotted (which tables, how many rows each, a per-table content digest,
and the alembic head the snapshot was taken against). The operator's cron
moves the manifest + the snapshot content files into a separate, encrypted
failure domain (runbook §9.1); recovery reloads the snapshot into an empty
DB and the manifest's digests let :mod:`backup.verify` prove the reload is
byte-faithful to the backup point.

Shape alignment with ADR-0005 §10.2 archive manifest: ``backup_id`` /
``content_digest`` / ``schema_version`` mirror the archive ``batch_id`` /
``content_digest`` / ``schema_versions`` so a future "backup-vs-archive
consistency" check (ADR-0005 §15: "备份恢复后归档水位与摘要抽样一致")
can compare the two evidence layers without a translation step.

Tamper evidence
---------------

``manifest_digest`` is a sha256 computed over the manifest body **with the
field itself blanked out**. A snapshot file edited on disk (operator
error, a compromised backup target) produces a different content digest on
verify, and a manifest edited to hide that produces a ``manifest_digest``
that no longer matches the recomputed one — either breaks the chain. The
digest is recomputed on every load (:func:`verify_manifest_integrity`) so
the evidence is self-validating, not reliant on the Job having run cleanly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: Filename the latest manifest is written to inside ``destination_dir``.
#: Fixed (not timestamped) so :func:`latest_manifest` and the doctor probe
#: can locate it deterministically; prior snapshots are preserved as
#: timestamped sidecars by the CLI if retention is wanted (out of scope here).
MANIFEST_FILENAME = "manifest.json"

#: The top-level content digest field; blanked while computing
#: ``manifest_digest`` so the digest does not hash itself.
_CONTENT_DIGEST_FIELD = "content_digest"
_MANIFEST_DIGEST_FIELD = "manifest_digest"


class BackupTableEntry(BaseModel):
    """One table's snapshot evidence inside a manifest."""

    name: str
    row_count: int
    #: sha256 over the table's normalised row stream (see
    #: :mod:`backup.snapshot`). Stable across backends and reruns for the
    #: same rows, so a byte-faithful restore reproduces it exactly.
    content_digest: str
    #: Column keys in mapper/insert order, so a restore writes rows back in a
    #: shape that round-trips. Stored (not just recomputed) so a restore does
    #: not depend on the current ORM mapping having the same column order.
    columns: list[str]

    model_config = {"extra": "forbid"}


class BackupManifest(BaseModel):
    """Evidence manifest for one application-level backup snapshot.

    See module docstring for the tamper-evidence design and the alignment
    with ADR-0005 §10.2 archive manifests.
    """

    backup_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime
    #: Database backend the snapshot was taken from (``postgres`` / ``sqlite``).
    #: Restoring across backends is supported (the snapshot is backend-neutral
    #: normalised JSON), and recording the source backend surfaces a cross-
    #: backend restore to the operator.
    backend: str
    #: The ``alembic_version.version_num`` head row at snapshot time (e.g.
    #: ``0011_audit_outbox``). A restore into a DB whose alembic head differs
    #: is a schema-drift FAIL in :mod:`backup.verify`.
    schema_version: str
    declared_rpo_hours: int
    tables: list[BackupTableEntry]
    content_digest: str = ""
    manifest_digest: str = ""

    model_config = {"extra": "forbid"}


def _stable_json(payload: Any) -> str:
    """Deterministic JSON for hashing (sorted keys, no whitespace)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def compute_content_digest(table_entries: list[dict[str, Any]]) -> str:
    """sha256 over the table entries in a fixed, sort-stable order.

    The per-table ``content_digest`` (computed in :mod:`snapshot`) already
    pins each table; this rolls them into one manifest-level digest so a
    single edit anywhere in the snapshot surfaces as a manifest mismatch.
    Tables are re-sorted by name here (defence-in-depth against list-order
    drift) and only the tamper-relevant fields are hashed (``name`` /
    ``row_count`` / ``content_digest``) — ``columns`` order is already
    captured in the per-table digest.
    """
    material = [{"name": t["name"], "row_count": t["row_count"], "content_digest": t["content_digest"]} for t in sorted(table_entries, key=lambda t: t["name"])]
    return hashlib.sha256(_stable_json(material).encode("utf-8")).hexdigest()


def compute_manifest_digest(manifest_body: dict[str, Any]) -> str:
    """sha256 over the manifest with both digest fields blanked.

    Blanking both means a recomputation is stable regardless of whether the
    caller has already populated the digests; the verify path recomputes
    against a body that carries them and the match still holds.
    """
    body = dict(manifest_body)
    body[_CONTENT_DIGEST_FIELD] = ""
    body[_MANIFEST_DIGEST_FIELD] = ""
    return hashlib.sha256(_stable_json(body).encode("utf-8")).hexdigest()


def finalize_digests(manifest: BackupManifest) -> BackupManifest:
    """Compute + stamp ``content_digest`` then ``manifest_digest``.

    Idempotent: re-running on an already-finalised manifest reproduces the
    same digests (both are pure functions of the body). Returns a new model
    instance so the caller can keep the pre-finalise copy if needed.
    """
    body = manifest.model_dump(mode="json")
    # created_at serialises to ISO; preserve the encoded form so the digest
    # is over exactly what is written to disk.
    content = compute_content_digest(body["tables"])
    body[_CONTENT_DIGEST_FIELD] = content
    body[_MANIFEST_DIGEST_FIELD] = compute_manifest_digest(body)
    return BackupManifest.model_validate(body)


def write_manifest(destination_dir: Path, manifest: BackupManifest) -> Path:
    """Write the manifest to ``destination_dir/manifest.json`` (atomic).

    Returns the written path. Parent dir is created if missing. Atomic via
    write-then-rename so a crash mid-write cannot leave a half manifest that
    :func:`latest_manifest` would pick up as evidence.
    """
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / MANIFEST_FILENAME
    tmp = target.with_suffix(".json.tmp")
    payload = manifest.model_dump(mode="json")
    tmp.write_text(_stable_json(payload) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def load_manifest(path: Path) -> BackupManifest:
    """Load + validate a manifest from ``path``.

    Raises ``ValueError`` if the file is not valid manifest JSON or fails
    pydantic validation — the doctor probe and restore CLI turn this into a
    FAIL rather than crashing.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read manifest at {path}: {exc}") from exc
    try:
        return BackupManifest.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        raise ValueError(f"manifest at {path} failed validation: {exc}") from exc


def verify_manifest_integrity(path: Path) -> tuple[bool, BackupManifest]:
    """Recompute both digests and compare against the on-disk manifest.

    Returns ``(ok, manifest)``. ``ok`` is False if the file is unreadable,
    fails validation, or either recomputed digest differs from the stored
    value — any of which means the manifest or its snapshot was altered after
    the Job wrote it (operator error, a tampered backup target). The manifest
    is still returned (when loadable) so the caller can report which check
    failed against the recorded values.
    """
    path = Path(path)
    try:
        manifest = load_manifest(path)
    except ValueError:
        return False, None  # type: ignore[return-value]

    body = manifest.model_dump(mode="json")
    expected_content = compute_content_digest(body["tables"])
    expected_manifest = compute_manifest_digest(body)
    ok = expected_content == manifest.content_digest and expected_manifest == manifest.manifest_digest
    return ok, manifest


_MANIFEST_TIMESTAMP_RE = re.compile(r"^manifest-(?P<ts>\d{8}T\d{6}Z)\.json$")


def latest_manifest(destination_dir: Path) -> BackupManifest | None:
    """Return the newest manifest in ``destination_dir`` (by created_at).

    The canonical current manifest is ``manifest.json``; timestamped
    ``manifest-YYYYMMDDTHHMMSSZ.json`` sidecars (written by the CLI retention
    step) are also recognised and take precedence if newer. Returns ``None``
    if no manifest exists.
    """
    destination_dir = Path(destination_dir)
    candidates: list[tuple[datetime, Path]] = []
    canonical = destination_dir / MANIFEST_FILENAME
    if canonical.exists():
        try:
            m = load_manifest(canonical)
            candidates.append((m.created_at, canonical))
        except ValueError:
            logger.warning("canonical manifest at %s is unreadable; ignoring", canonical)
    for child in destination_dir.glob("manifest-*.json"):
        match = _MANIFEST_TIMESTAMP_RE.match(child.name)
        if match is None:
            continue
        try:
            m = load_manifest(child)
        except ValueError:
            continue
        # Prefer the manifest's own created_at (source of truth) over the
        # filename timestamp, but fall back to the filename if created_at is
        # missing/malformed for robustness against hand-edited sidecars.
        ts = m.created_at
        candidates.append((ts, child))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return load_manifest(candidates[-1][1])


__all__ = [
    "MANIFEST_FILENAME",
    "BackupManifest",
    "BackupTableEntry",
    "compute_content_digest",
    "compute_manifest_digest",
    "finalize_digests",
    "latest_manifest",
    "load_manifest",
    "verify_manifest_integrity",
    "write_manifest",
]
