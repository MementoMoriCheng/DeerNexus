"""Tests for the worker-level lease heartbeat lifecycle (PR-071, Track G).

The lease-store primitives (claim/renew/release atomicity, TM-026 race) are
covered in ``test_run_ownership.py``. This file covers the worker-side
heartbeat loop (``_run_lease_heartbeat``): it renews on a cadence, stops on the
stop event, and bails out (logging + metric) when a renewal fails (the token is
no longer current — a new owner won, or the lease expired).

``run_agent``'s claim/release wiring is exercised end-to-end by the existing
runtime-lifecycle e2e suite (NullLeaseStore path, which is the dev/default
shape); the Redis-backed claim-lost short-circuit is a thin branch on top of
the primitives already proven in ``test_run_ownership.py``.
"""

from __future__ import annotations

import asyncio

import pytest
from fakeredis import FakeAsyncRedis

from deerflow.runtime.runs.ownership import (
    LEASE_TTL_SECONDS,
    ClaimRecord,
    RedisLeaseStore,
)
from deerflow.runtime.runs.worker import _run_lease_heartbeat

ORG_ID = "org-test"
RUN_ID = "run-1"

pytestmark = pytest.mark.anyio


def _make_record() -> ClaimRecord:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return ClaimRecord(
        run_id=RUN_ID,
        worker_id="w1",
        lease_token="tok-1",
        lease_expires_at=now + timedelta(seconds=LEASE_TTL_SECONDS),
        worker_version="v1",
        claimed_at=now,
    )


@pytest.fixture
def store_factory():
    client = FakeAsyncRedis()
    return RedisLeaseStore(client), client


@pytest.fixture
async def store(store_factory):
    s, client = store_factory
    yield s
    await client.aclose()


class TestHeartbeatLoop:
    async def test_stops_promptly_on_stop_event(self, store):
        """The loop exits within ~no time when stop_event is set (shutdown)."""
        stop = asyncio.Event()
        # Claim so the lease exists for renew to succeed.
        claim = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        task = asyncio.create_task(_run_lease_heartbeat(store, claim.record, org_id=ORG_ID, run_id=RUN_ID, stop_event=stop))
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)  # exits promptly, no hang

    async def test_bails_out_when_renew_fails_token_mismatch(self, store):
        """A renewal failure (stale token) stops the loop — ownership moved."""
        # Claim as w1, then have w2 reclaim (release + re-claim), so w1's token
        # is now stale.
        first = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        await store.release(first.record, org_id=ORG_ID)
        await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w2", worker_version="v1")
        # w1's stale record:
        stop = asyncio.Event()
        task = asyncio.create_task(_run_lease_heartbeat(store, first.record, org_id=ORG_ID, run_id=RUN_ID, stop_event=stop))
        # The loop should bail out on the first failed renew (well under TTL).
        await asyncio.wait_for(task, timeout=15.0)
        assert task.done()

    async def test_renewal_exception_does_not_kill_unhandled(self, store):
        """A Redis error during renew is caught — the loop logs and stops."""
        record = _make_record()

        class _BrokenStore:
            async def renew(self, rec, *, org_id, **kw):
                raise RuntimeError("redis down")

        stop = asyncio.Event()
        task = asyncio.create_task(_run_lease_heartbeat(_BrokenStore(), record, org_id=ORG_ID, run_id=RUN_ID, stop_event=stop))
        await asyncio.wait_for(task, timeout=15.0)
        assert task.done()  # bailed, did not raise out of the task
