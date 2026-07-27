"""RunRepository.put release-pin behaviour (PR-056, ART-1740).

Tests the persistence invariant that ``start_run`` ultimately relies on: the
ReleaseRef columns are written **insert-only** (a later status update never
clobbers a frozen pin), ``legacy_unpinned`` defaults to true, and a retried put
preserves the originally-pinned identity. These run against a real migrated
SQLite DB (no HTTP stack) so they are deterministic and fast.

The full ``start_run → resolver.resolve()`` path is exercised end-to-end by
``test_runtime_lifecycle_e2e``; here we isolate the storage contract that
makes the pin trustworthy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio

_ORG_ID = "org-pin-test"


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'pin.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


@pytest.fixture
async def seeded_org(sf):
    from deerflow.persistence.orgs.model import OrganizationRow

    async with sf() as session:
        session.add(OrganizationRow(id=_ORG_ID, name=_ORG_ID, slug=_ORG_ID, status="active"))
        await session.commit()


async def _get_row(sf, run_id):
    from deerflow.persistence.run.model import RunRow

    async with sf() as session:
        row = await session.get(RunRow, run_id)
        return None if row is None else row.to_dict()


class TestPutReleasePin:
    async def test_new_run_defaults_to_legacy_unpinned(self, sf, seeded_org):
        """A run written without a pin is legacy_unpinned=True and no release columns."""
        from deerflow.persistence.run import RunRepository

        repo = RunRepository(sf)
        await repo.put(
            "run-legacy",
            thread_id="thread-1",
            assistant_id="lead_agent",
            org_id=_ORG_ID,
        )
        row = await _get_row(sf, "run-legacy")
        assert row["legacy_unpinned"] is True
        assert row["release_version_id"] is None
        assert row["release_digest"] is None

    async def test_pinned_run_records_frozen_release_ref(self, sf, seeded_org):
        """start_run passes the resolved ReleaseRef; put persists all 5 fields."""
        from deerflow.persistence.run import RunRepository

        repo = RunRepository(sf)
        await repo.put(
            "run-pinned",
            thread_id="thread-1",
            assistant_id="lead_agent",
            org_id=_ORG_ID,
            release_package_id="pkg-1",
            release_version_id="ver-1",
            release_channel="prod",
            release_digest="sha256:abc",
            legacy_unpinned=False,
        )
        row = await _get_row(sf, "run-pinned")
        assert row["legacy_unpinned"] is False
        assert row["release_package_id"] == "pkg-1"
        assert row["release_version_id"] == "ver-1"
        assert row["release_channel"] == "prod"
        assert row["release_digest"] == "sha256:abc"

    async def test_pin_is_insert_only_status_update_does_not_clobber(self, sf, seeded_org):
        """A follow-up status update (RunManager retries put) must NOT overwrite the pin.

        This is the core §6 step-9 invariant: the execution phase consumes the
        persisted ReleaseRef and never re-reads the channel. If a status update
        could clear release_version_id, a later promote/rollback would silently
        mutate what the run already executed against.
        """
        from deerflow.persistence.run import RunRepository

        repo = RunRepository(sf)
        # First put: pin the run.
        await repo.put(
            "run-frozen",
            thread_id="thread-1",
            assistant_id="lead_agent",
            org_id=_ORG_ID,
            release_package_id="pkg-1",
            release_version_id="ver-1",
            release_channel="prod",
            release_digest="sha256:abc",
            legacy_unpinned=False,
        )
        # Second put: a status follow-up that omits the pin kwargs (the common
        # retry / update_status path). The pin must survive untouched.
        await repo.put(
            "run-frozen",
            thread_id="thread-1",
            assistant_id="lead_agent",
            org_id=_ORG_ID,
            status="running",
        )
        row = await _get_row(sf, "run-frozen")
        assert row["status"] == "running"
        assert row["legacy_unpinned"] is False
        assert row["release_version_id"] == "ver-1"
        assert row["release_digest"] == "sha256:abc"

    async def test_retried_put_preserves_pin(self, sf, seeded_org):
        """A retried put (RunManager's transient-failure retry) keeps the pin.

        Mirrors the org_id insert-only contract: the retry path omits the pin
        kwargs, so they must be preserved from the original insert.
        """
        from deerflow.persistence.run import RunRepository

        repo = RunRepository(sf)
        await repo.put(
            "run-retry",
            thread_id="thread-1",
            assistant_id="lead_agent",
            org_id=_ORG_ID,
            release_package_id="pkg-1",
            release_version_id="ver-1",
            release_channel="prod",
            release_digest="sha256:abc",
            legacy_unpinned=False,
        )
        # Retry put (same run_id, pin kwargs omitted by the caller).
        await repo.put(
            "run-retry",
            thread_id="thread-1",
            assistant_id="lead_agent",
            org_id=_ORG_ID,
        )
        row = await _get_row(sf, "run-retry")
        assert row["release_version_id"] == "ver-1"
        assert row["legacy_unpinned"] is False
