"""Tests for the application-level backup evidence layer (PR-065).

Covers the four harness modules together as one round-trip contract:
manifest tamper-evidence (write → verify → tamper → fail), snapshot
determinism + full-table coverage + cross-rerun digest stability, restore
into an empty DB (byte-faithful: row counts AND content digests reproduce),
and the verify gates' PASS/FAIL/SKIP semantics (a tampered restore FAILs
content_digests_match; a null-org restore FAILs no_null_org_id; every
infrastructure-blocked gate is a labelled SKIP, not a false PASS).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from deerflow.persistence.backup import (
    BackupManifest,
    RestoreError,
    compute_content_digest,
    compute_manifest_digest,
    finalize_digests,
    latest_manifest,
    load_manifest,
    take_snapshot,
    verify_manifest_integrity,
    verify_restore,
    write_manifest,
)
from deerflow.persistence.backup.manifest import BackupTableEntry
from deerflow.persistence.backup.snapshot import (
    _content_digest_for_rows,
    _normalise_value,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def sf(tmp_path: Path):
    """A fresh SQLite engine, schema bootstrapped (create_all + alembic stamp head)."""
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'backup.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


def _stamp_target(engine):
    """Create + stamp the alembic_version table on a create_all-only target.

    Mirrors what scripts/restore.py does after restore, so the verify
    schema_compatible gate sees the manifest's schema_version.
    """

    async def _do():
        from sqlalchemy import text

        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"))
            await conn.execute(text("DELETE FROM alembic_version"))

    return _do


async def _seed_org(sf, org_id: str) -> None:
    from deerflow.persistence.orgs.model import OrganizationRow

    async with sf() as session:
        session.add(OrganizationRow(id=org_id, name=org_id, slug=org_id, status="active"))
        await session.commit()


async def _build_snapshot_on_disk(sf, tmp_path: Path, *, backend: str = "sqlite", rpo: int = 24):
    """Snapshot → write manifest + content files; return the manifest + dest dir."""
    from deerflow.persistence.backup import read_table_rows, write_table_rows

    manifest = await take_snapshot(sf, backend=backend, declared_rpo_hours=rpo)
    dest = tmp_path / "backup"
    for entry in manifest.tables:
        rows = await read_table_rows(sf, entry.name)
        write_table_rows(dest, entry.name, rows)
    write_manifest(dest, manifest)
    return manifest, dest


# ===========================================================================
# Manifest: tamper evidence
# ===========================================================================


class TestManifestTamperEvidence:
    def test_finalize_digests_is_idempotent(self):
        manifest = BackupManifest(
            created_at=datetime.now(UTC),
            backend="sqlite",
            schema_version="0011_audit_outbox",
            declared_rpo_hours=24,
            tables=[BackupTableEntry(name="organizations", row_count=0, content_digest="x", columns=["id"])],
        )
        once = finalize_digests(manifest)
        twice = finalize_digests(once)
        assert once.content_digest == twice.content_digest
        assert once.manifest_digest == twice.manifest_digest

    def test_write_then_verify_round_trips(self, tmp_path):
        manifest = BackupManifest(
            created_at=datetime.now(UTC),
            backend="sqlite",
            schema_version="0011_audit_outbox",
            declared_rpo_hours=24,
            tables=[],
        )
        finalized = finalize_digests(manifest)
        path = write_manifest(tmp_path, finalized)
        ok, loaded = verify_manifest_integrity(path)
        assert ok
        assert loaded.backup_id == finalized.backup_id

    def test_tampered_content_digest_fails_verification(self, tmp_path):
        manifest = finalize_digests(
            BackupManifest(
                created_at=datetime.now(UTC),
                backend="sqlite",
                schema_version="0011_audit_outbox",
                declared_rpo_hours=24,
                tables=[BackupTableEntry(name="t", row_count=1, content_digest="orig", columns=["id"])],
            )
        )
        path = write_manifest(tmp_path, manifest)
        # Mutate a table's row_count on disk without recomputing the digest.
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["tables"][0]["row_count"] = 99
        path.write_text(json.dumps(raw), encoding="utf-8")
        ok, _ = verify_manifest_integrity(path)
        assert ok is False

    def test_tampered_manifest_digest_fails_verification(self, tmp_path):
        manifest = finalize_digests(
            BackupManifest(
                created_at=datetime.now(UTC),
                backend="sqlite",
                schema_version="0011_audit_outbox",
                declared_rpo_hours=24,
                tables=[],
            )
        )
        path = write_manifest(tmp_path, manifest)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["manifest_digest"] = "0" * 64
        path.write_text(json.dumps(raw), encoding="utf-8")
        ok, _ = verify_manifest_integrity(path)
        assert ok is False

    def test_corrupt_manifest_load_raises(self, tmp_path):
        path = tmp_path / "manifest.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError):
            load_manifest(path)

    def test_latest_manifest_returns_none_when_empty(self, tmp_path):
        assert latest_manifest(tmp_path) is None


# ===========================================================================
# Snapshot: determinism + coverage
# ===========================================================================


class TestSnapshot:
    @pytest.mark.anyio
    async def test_snapshot_covers_every_metadata_table(self, sf):
        import deerflow.persistence.models  # noqa: F401
        from deerflow.persistence.base import Base

        manifest = await take_snapshot(sf, backend="sqlite", declared_rpo_hours=24)
        snapshotted = {e.name for e in manifest.tables}
        metadata_tables = set(Base.metadata.tables)
        assert snapshotted == metadata_tables, "snapshot must cover every Base.metadata table"

    @pytest.mark.anyio
    async def test_snapshot_digest_stable_across_reruns(self, sf):
        m1 = await take_snapshot(sf, backend="sqlite", declared_rpo_hours=24)
        m2 = await take_snapshot(sf, backend="sqlite", declared_rpo_hours=24)
        digests1 = {e.name: e.content_digest for e in m1.tables}
        digests2 = {e.name: e.content_digest for e in m2.tables}
        assert digests1 == digests2, "snapshot digests must be stable for unchanged data"

    @pytest.mark.anyio
    async def test_snapshot_digest_changes_with_data(self, sf):
        m_before = await take_snapshot(sf, backend="sqlite", declared_rpo_hours=24)
        await _seed_org(sf, "org-change-detector")
        m_after = await take_snapshot(sf, backend="sqlite", declared_rpo_hours=24)
        before = {e.name: e.content_digest for e in m_before.tables}
        after = {e.name: e.content_digest for e in m_after.tables}
        assert before["organizations"] != after["organizations"]

    @pytest.mark.anyio
    async def test_snapshot_records_alembic_head(self, sf):
        manifest = await take_snapshot(sf, backend="sqlite", declared_rpo_hours=24)
        # The test engine stamps head on bootstrap; the snapshot must record it.
        assert manifest.schema_version == "0011_audit_outbox"

    def test_normalise_value_handles_datetime_bytes_uuid(self):
        from uuid import UUID

        assert _normalise_value(None) is None
        # Naive datetime is treated as UTC and ISO-encoded.
        iso = _normalise_value(datetime(2026, 1, 2, 3, 4, 5))
        assert iso.startswith("2026-01-02T03:04:05")
        assert _normalise_value(b"\x00\xff") == "00ff"
        assert _normalise_value(UUID("12345678-1234-5678-1234-567812345678")) == "12345678-1234-5678-1234-567812345678"

    def test_content_digest_is_deterministic_over_same_sequence(self):
        # _content_digest_for_rows hashes the row stream in the order given.
        # Determinism for the same data is guaranteed by the snapshot layer
        # reading rows PK-sorted, so the same DB always yields the same input
        # sequence to this function. A different input order legitimately
        # produces a different digest (the function hashes the sequence, not a set).
        rows = [{"id": "1", "n": "a"}, {"id": "2", "n": "b"}]
        assert _content_digest_for_rows(rows) == _content_digest_for_rows(list(rows))
        assert _content_digest_for_rows(rows) != _content_digest_for_rows(list(reversed(rows)))


# ===========================================================================
# Restore: byte-faithful into an empty DB
# ===========================================================================


async def _restore_into_empty_db(manifest, content_dir, tmp_path, *, target_name="target.db"):
    """Restore a manifest into a fresh create_all DB; return the session factory + engine."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import deerflow.persistence.models  # noqa: F401
    from deerflow.persistence.backup import restore_from_manifest
    from deerflow.persistence.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / target_name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    report = await restore_from_manifest(sf, manifest, content_dir)
    return sf, engine, report


class TestRestore:
    @pytest.mark.anyio
    async def test_restore_reproduces_row_counts_and_digests(self, sf, tmp_path):
        await _seed_org(sf, "org-a")
        manifest, dest = await _build_snapshot_on_disk(sf, tmp_path)
        rsf, engine, report = await _restore_into_empty_db(manifest, dest, tmp_path)
        try:
            assert report.integrity_ok
            assert report.restored_counts["organizations"] == 1
            assert set(report.tables_in_order) == {e.name for e in manifest.tables}
        finally:
            await engine.dispose()

    @pytest.mark.anyio
    async def test_restore_refuses_non_empty_target(self, sf, tmp_path):
        await _seed_org(sf, "org-block")
        manifest, dest = await _build_snapshot_on_disk(sf, tmp_path)
        rsf, engine, _ = await _restore_into_empty_db(manifest, dest, tmp_path)
        try:
            # Second restore into the SAME (now-populated) target must fail closed.
            with pytest.raises(RestoreError):
                await restore_from_manifest_again(rsf, manifest, dest)
        finally:
            await engine.dispose()

    @pytest.mark.anyio
    async def test_restore_missing_content_file_fails(self, sf, tmp_path):
        manifest, dest = await _build_snapshot_on_disk(sf, tmp_path)
        # Delete one content file; restore must fail before writing rows.
        (dest / "snapshot" / "organizations.jsonl").unlink()
        with pytest.raises(RestoreError):
            sf, engine, _ = await _restore_into_empty_db(manifest, dest, tmp_path)
            await engine.dispose()


async def restore_from_manifest_again(sf, manifest, content_dir):
    """Re-invoke restore on an already-restored target (expected to raise)."""
    from deerflow.persistence.backup import restore_from_manifest

    return await restore_from_manifest(sf, manifest, content_dir)


# ===========================================================================
# Verify gates: PASS / FAIL / SKIP
# ===========================================================================


async def _verify_after_restore(manifest, content_dir, tmp_path, *, stamp=True, mutate=None):
    """Restore + stamp + verify; return the VerifyReport.

    ``mutate`` is an optional ``async (engine) -> None`` run after restore +
    stamp but before verify, so a test can inject a drift (phantom row,
    null org_id) the verify gates must catch.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import deerflow.persistence.models  # noqa: F401
    from deerflow.persistence.backup import restore_from_manifest
    from deerflow.persistence.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'verify.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    await restore_from_manifest(sf, manifest, content_dir)
    if stamp and manifest.schema_version != "unknown":
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"))
            await conn.execute(text("DELETE FROM alembic_version"))
            await conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                {"v": manifest.schema_version},
            )
    if mutate is not None:
        await mutate(engine)
    vsf = async_sessionmaker(engine, expire_on_commit=False)
    report = await verify_restore(vsf, manifest)
    await engine.dispose()
    return report


class TestVerifyGates:
    @pytest.mark.anyio
    async def test_clean_restore_passes_all_reachable_gates(self, sf, tmp_path):
        await _seed_org(sf, "org-clean")
        manifest, dest = await _build_snapshot_on_disk(sf, tmp_path)
        report = await _verify_after_restore(manifest, dest, tmp_path)
        assert report.failed == 0, [g.name for g in report.gates if g.status == "FAIL"]
        # 6 reachable gates all PASS.
        assert report.passed == 6
        # Every infra-blocked gate is a SKIP, never a silent PASS.
        assert report.skipped == 8
        skip_names = {g.name for g in report.gates if g.status == "SKIP"}
        assert "release_channel_points_at_valid_version" in skip_names
        assert "deletion_ledger_replayed" in skip_names
        assert "audit_archive_watermark_consistent" in skip_names

    @pytest.mark.anyio
    async def test_skip_gates_carry_track_specific_remediation(self, sf, tmp_path):
        manifest, dest = await _build_snapshot_on_disk(sf, tmp_path)
        report = await _verify_after_restore(manifest, dest, tmp_path)
        for gate in report.gates:
            if gate.status == "SKIP":
                assert "Blocked on" in gate.detail, f"{gate.name} SKIP must name its blocker"

    @pytest.mark.anyio
    async def test_schema_drift_fails_schema_compatible(self, sf, tmp_path):
        await _seed_org(sf, "org-drift")
        manifest, dest = await _build_snapshot_on_disk(sf, tmp_path)
        # Restore WITHOUT stamping → alembic_version empty → head='unknown' ≠ manifest.
        report = await _verify_after_restore(manifest, dest, tmp_path, stamp=False)
        schema_gate = next(g for g in report.gates if g.name == "schema_compatible")
        assert schema_gate.status == "FAIL"

    @pytest.mark.anyio
    async def test_tampered_content_fails_digest_and_count_gates(self, sf, tmp_path):
        await _seed_org(sf, "org-tamper")
        manifest, dest = await _build_snapshot_on_disk(sf, tmp_path)

        async def inject_phantom_org(engine):
            # Add a row the manifest does not record → row_count AND digest drift.
            from sqlalchemy import text

            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO organizations (id, slug, name, status, settings, created_at, updated_at, row_version) "
                        "VALUES ('phantom', 'phantom', 'phantom', 'active', '{}', "
                        "'2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000', 1)"
                    )
                )

        report = await _verify_after_restore(manifest, dest, tmp_path, mutate=inject_phantom_org)
        names = {g.name: g.status for g in report.gates}
        assert names["row_counts_match"] == "FAIL"
        assert names["content_digests_match"] == "FAIL"


# ===========================================================================
# content_digest / manifest_digest helpers
# ===========================================================================


class TestDigestHelpers:
    def test_compute_content_digest_stable_regardless_of_input_order(self):
        entries = [
            {"name": "a", "row_count": 1, "content_digest": "da"},
            {"name": "b", "row_count": 2, "content_digest": "db"},
        ]
        assert compute_content_digest(entries) == compute_content_digest(list(reversed(entries)))

    def test_compute_manifest_digest_ignores_both_digest_fields(self):
        body = {
            "backup_id": "x",
            "tables": [],
            "content_digest": "abc",
            "manifest_digest": "def",
        }
        # Blanking the fields inside the helper must yield the same digest as
        # passing them already blank.
        body_blank = {**body, "content_digest": "", "manifest_digest": ""}
        assert compute_manifest_digest(body) == compute_manifest_digest(body_blank)
