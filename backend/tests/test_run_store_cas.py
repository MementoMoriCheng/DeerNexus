"""CAS tests for the run store (PR-070).

Covers the compare-and-set contract added to ``update_status`` /
``update_run_completion`` on both the in-memory store (fast path) and the SQL
repository (the real ``WHERE row_version = :expected`` + ``rowcount`` path).
The memory store implements an in-memory CAS check so the same contract is
exercisable without a DB; the SQL repository is the production CAS caller.

CAS contract (PR-070):
* ``expected_row_version`` matching the current row → write succeeds,
  ``row_version`` bumped by 1.
* ``expected_row_version`` stale (a concurrent writer already bumped it) →
  write returns ``False``, row is **not** mutated (terminal-immutability fence).
* ``expected_row_version=None`` (default) → unconditional write (backward
  compatibility: pre-PR-070 callers see no behavior change).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
from deerflow.persistence.run.sql import RunRepository
from deerflow.runtime.runs.store.memory import MemoryRunStore

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Memory store CAS
# ---------------------------------------------------------------------------


class TestMemoryStoreCAS:
    async def test_update_status_cas_match_succeeds_and_bumps(self):
        store = MemoryRunStore()
        await store.put("r1", thread_id="t1", status="pending")
        assert await store.update_status("r1", "running", expected_row_version=1) is True
        run = await store.get("r1")
        assert run["status"] == "running"
        assert run["row_version"] == 2

    async def test_update_status_cas_stale_returns_false_no_mutation(self):
        store = MemoryRunStore()
        await store.put("r1", thread_id="t1", status="running")
        # Bump to 2 (a concurrent writer won).
        await store.update_status("r1", "running", expected_row_version=1)
        # A stale CAS expecting version 1 must lose and not mutate.
        updated = await store.update_status("r1", "error", expected_row_version=1)
        assert updated is False
        run = await store.get("r1")
        assert run["status"] == "running"  # unchanged
        assert run["row_version"] == 2  # unchanged

    async def test_update_status_no_expected_is_unconditional(self):
        """expected_row_version=None (default) bypasses CAS — backward compatible."""
        store = MemoryRunStore()
        await store.put("r1", thread_id="t1", status="running")
        # Even though the row is at version 1, an unconditional write succeeds
        # and does NOT bump the version.
        assert await store.update_status("r1", "error") is True
        run = await store.get("r1")
        assert run["status"] == "error"
        assert run["row_version"] == 1  # unconditional write does not bump

    async def test_update_status_unknown_run_returns_false(self):
        store = MemoryRunStore()
        assert await store.update_status("nope", "running", expected_row_version=1) is False

    async def test_update_run_completion_cas_match_bumps(self):
        store = MemoryRunStore()
        await store.put("r1", thread_id="t1", status="running")
        ok = await store.update_run_completion("r1", status="success", total_tokens=100, expected_row_version=1)
        assert ok is True
        run = await store.get("r1")
        assert run["status"] == "success"
        assert run["total_tokens"] == 100
        assert run["row_version"] == 2

    async def test_update_run_completion_cas_stale_returns_false(self):
        store = MemoryRunStore()
        await store.put("r1", thread_id="t1", status="running")
        await store.update_status("r1", "running", expected_row_version=1)  # now v2
        ok = await store.update_run_completion(
            "r1",
            status="success",
            expected_row_version=1,  # stale
        )
        assert ok is False
        run = await store.get("r1")
        assert run["status"] == "running"  # completion did not land

    async def test_row_version_preserved_across_retry_put(self):
        """A retry put() must not reset row_version back to 1 (CAS must survive retries)."""
        store = MemoryRunStore()
        await store.put("r1", thread_id="t1", status="running")
        await store.update_status("r1", "running", expected_row_version=1)  # v2
        # Retry the put (idempotent insert/update) — row_version must stay 2.
        await store.put("r1", thread_id="t1", status="running")
        run = await store.get("r1")
        assert run["row_version"] == 2


# ---------------------------------------------------------------------------
# SQL repository CAS (the real WHERE row_version = :expected path)
# ---------------------------------------------------------------------------


@pytest.fixture
async def repo(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'run_cas.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        sf = get_session_factory()
        # runs.org_id has a FK → organizations (RESTRICT). The conftest autouse
        # fixture binds the default tenant so put() resolves org_id="default";
        # seed that org so the run insert satisfies the FK.
        from deerflow.persistence.orgs.model import OrganizationRow

        async with sf() as session:
            session.add(OrganizationRow(id="default", slug="default", name="default", status="active"))
            await session.commit()
        yield RunRepository(sf)
    finally:
        await close_engine()


class TestSqlRepositoryCAS:
    async def test_update_status_cas_match_bumps_row_version(self, repo):
        await repo.put("r1", thread_id="t1", status="pending")
        assert await repo.update_status("r1", "running", expected_row_version=1) is True
        run = await repo.get("r1")
        assert run["status"] == "running"
        assert run["row_version"] == 2

    async def test_update_status_cas_stale_returns_false(self, repo):
        await repo.put("r1", thread_id="t1", status="running")
        await repo.update_status("r1", "running", expected_row_version=1)  # v2
        updated = await repo.update_status("r1", "error", expected_row_version=1)  # stale
        assert updated is False
        run = await repo.get("r1")
        assert run["status"] == "running"
        assert run["row_version"] == 2

    async def test_update_status_no_expected_is_unconditional(self, repo):
        await repo.put("r1", thread_id="t1", status="running")
        assert await repo.update_status("r1", "error") is True
        run = await repo.get("r1")
        assert run["status"] == "error"
        # Unconditional write does not bump (no CAS predicate).
        assert run["row_version"] == 1

    async def test_update_run_completion_cas_match_bumps(self, repo):
        await repo.put("r1", thread_id="t1", status="running")
        ok = await repo.update_run_completion("r1", status="success", total_tokens=42, expected_row_version=1)
        assert ok is True
        run = await repo.get("r1")
        assert run["status"] == "success"
        assert run["total_tokens"] == 42
        assert run["row_version"] == 2

    async def test_update_run_completion_cas_stale_returns_false(self, repo):
        await repo.put("r1", thread_id="t1", status="running")
        await repo.update_status("r1", "running", expected_row_version=1)  # v2
        ok = await repo.update_run_completion("r1", status="success", expected_row_version=1)
        assert ok is False
        run = await repo.get("r1")
        assert run["status"] == "running"  # not overwritten

    async def test_unknown_run_cas_returns_false(self, repo):
        assert await repo.update_status("nope", "running", expected_row_version=1) is False
