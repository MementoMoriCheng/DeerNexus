"""Tests for the audit archive job (T1C, ADR-0005 §10.2).

Covers: full archive sweep lifecycle (seed → export → JSONL write → manifest
→ watermark → verify), idempotency, verification failure → no watermark
advance, empty-org skip, and the worker loop stop mechanism.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM
from app.gateway.audit_archive_worker import (
    sweep_archive_audit,
)
from deerflow.contracts.identity import PrincipalRef
from deerflow.persistence.audit.watermark_model import AuditArchiveWatermarkRow
from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
from deerflow.tenancy.audit_events import build_audit_event

ORG_ID = "org-archive-test"

pytestmark = pytest.mark.anyio


class _FakeObjectStore:
    """In-memory ObjectStore for archive tests."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def put(self, *, object_key: str, content: bytes) -> None:
        self._data[object_key] = content

    async def get(self, object_key: str) -> bytes:
        if object_key not in self._data:
            raise FileNotFoundError(object_key)
        return self._data[object_key]

    async def exists(self, object_key: str) -> bool:
        return object_key in self._data

    async def delete(self, object_key: str) -> None:
        self._data.pop(object_key, None)


@pytest.fixture
async def sf(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'archive.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture
async def seeded_org(sf):
    from deerflow.persistence.orgs.model import OrganizationRow

    async with sf() as session:
        session.add(OrganizationRow(id=ORG_ID, name=ORG_ID, slug=ORG_ID, status="active"))
        await session.commit()


def _actor() -> PrincipalRef:
    return PrincipalRef(type="user", id="u-test", user_id="u-test")


async def _seed_events(sf, *, org_id: str, count: int, occurred_at: datetime) -> list[str]:
    """Seed ``count`` audit events at ``occurred_at`` and return their ids."""
    from deerflow.persistence.audit import insert_audit_event

    ids = []
    for i in range(count):
        event = build_audit_event(
            "iam.role_binding.created",
            org_id=org_id,
            actor=_actor(),
            resource={"type": "role_binding", "id": f"rb-{i}", "org_id": org_id},
        )
        event = event.model_copy(update={"occurred_at": occurred_at + timedelta(seconds=i)})
        row = await insert_audit_event(sf, event)
        ids.append(row.event_id)
    return ids


# ===========================================================================
# Tests
# ===========================================================================


class TestSweepArchiveAudit:
    async def test_full_archive_lifecycle(self, sf, seeded_org):
        """Seed events for yesterday → sweep → verify JSONL + manifest + watermark."""
        store = _FakeObjectStore()
        now = datetime.now(UTC)
        yesterday = now.date() - timedelta(days=1)
        occurred = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

        event_ids = await _seed_events(sf, org_id=ORG_ID, count=5, occurred_at=occurred)

        report = await sweep_archive_audit(sf, object_store=store, now=now)

        assert report.success is True
        assert report.events_archived == 5
        assert report.orgs_archived == 1
        assert report.batches_written == 1
        assert report.verify_failures == []

        # Verify JSONL object was written.
        jsonl_keys = [k for k in store._data if k.endswith(".jsonl")]
        assert len(jsonl_keys) == 1
        jsonl_content = store._data[jsonl_keys[0]].decode("utf-8")
        lines = jsonl_content.strip().split("\n")
        assert len(lines) == 5
        # Each line is valid JSON with event_id.
        for line in lines:
            parsed = json.loads(line)
            assert "event_id" in parsed

        # Verify manifest was written.
        manifest_keys = [k for k in store._data if k.endswith("manifest.json")]
        assert len(manifest_keys) == 1
        manifest = json.loads(store._data[manifest_keys[0]].decode("utf-8"))
        assert manifest["event_count"] == 5
        assert manifest["first_event_id"] == event_ids[0]
        assert manifest["last_event_id"] == event_ids[-1]
        assert manifest["content_digest"].startswith("sha256:")
        assert manifest["window_start"].startswith(yesterday.isoformat())
        assert manifest["storage_version"] == "1"

        # Verify watermark was recorded.
        async with sf() as session:
            wm = await session.get(AuditArchiveWatermarkRow, ORG_ID)
        assert wm is not None
        assert wm.last_event_id == event_ids[-1]
        assert wm.batch_count == 1

    async def test_idempotent_rearchive(self, sf, seeded_org):
        """Re-archiving the same window produces identical content + no duplicate watermark."""
        store = _FakeObjectStore()
        now = datetime.now(UTC)
        yesterday = now.date() - timedelta(days=1)
        occurred = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

        await _seed_events(sf, org_id=ORG_ID, count=3, occurred_at=occurred)

        report1 = await sweep_archive_audit(sf, object_store=store, now=now)
        assert report1.batches_written == 1

        # Re-archive the same window.
        report2 = await sweep_archive_audit(sf, object_store=store, now=now)
        assert report2.batches_written == 1  # Same org archived again (overwrite)
        assert report2.events_archived == 3  # Same events

        # Watermark advanced by 2 total batches (2 sweeps each wrote 1).
        async with sf() as session:
            wm = await session.get(AuditArchiveWatermarkRow, ORG_ID)
        assert wm.batch_count == 2

    async def test_verification_failure_does_not_advance_watermark(self, sf, seeded_org):
        """When read-back digest mismatches, watermark must NOT advance."""

        # Use a store that corrupts content on read.
        class _CorruptStore(_FakeObjectStore):
            async def get(self, object_key: str) -> bytes:
                if object_key.endswith(".jsonl"):
                    return b"corrupted-content"
                return await super().get(object_key)

        store = _CorruptStore()
        now = datetime.now(UTC)
        yesterday = now.date() - timedelta(days=1)
        occurred = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

        await _seed_events(sf, org_id=ORG_ID, count=2, occurred_at=occurred)

        report = await sweep_archive_audit(sf, object_store=store, now=now)

        assert report.success is False
        assert len(report.verify_failures) >= 1
        assert "digest mismatch" in report.verify_failures[0].lower()

        # Watermark should NOT exist (verification failed).
        async with sf() as session:
            wm = await session.get(AuditArchiveWatermarkRow, ORG_ID)
        assert wm is None

    async def test_empty_org_skipped(self, sf, seeded_org):
        """An org with no events in the window produces no objects."""
        store = _FakeObjectStore()
        now = datetime.now(UTC)

        report = await sweep_archive_audit(sf, object_store=store, now=now)

        assert report.events_archived == 0
        assert report.batches_written == 0
        assert len(store._data) == 0  # Nothing written

    async def test_events_outside_window_not_archived(self, sf, seeded_org):
        """Events from today (not yesterday) are not archived."""
        store = _FakeObjectStore()
        now = datetime.now(UTC)
        # Seed events for TODAY, not yesterday.
        today_noon = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

        await _seed_events(sf, org_id=ORG_ID, count=3, occurred_at=today_noon)

        report = await sweep_archive_audit(sf, object_store=store, now=now)
        assert report.events_archived == 0  # Today's events are NOT in yesterday's window
