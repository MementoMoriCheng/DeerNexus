"""Tests for the run ownership / lease primitives (PR-071, Track G).

Covers :mod:`deerflow.runtime.runs.ownership`:

* ``claim`` atomicity — SET NX means exactly one of two concurrent contenders
  wins (TM-026 multi-Worker same-Run mitigation).
* ``renew`` token gating — only the current ``lease_token`` may extend; a stale
  token (old owner, or a new owner after reclaim) is rejected (ADR §5.2).
* ``release`` token gating — only the current token holder may drop the key.
* lease TTL expiry — the key disappears after TTL, freeing the run for reclaim.
* ``NullLeaseStore`` — dev/single-replica no-op (claim always succeeds).
* ``make_lease_store`` — config URL → store selection.

The Redis-backed path is tested with ``fakeredis`` (in-process, no external
infra) so the suite stays hermetic; a separate ``@pytest.mark.real_redis`` test
exercises a live Redis at ``REDIS_URL`` (default ``localhost:6379``) and is
skipped when none is reachable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis import FakeAsyncRedis

from deerflow.runtime.runs.ownership import (
    HEARTBEAT_INTERVAL_SECONDS,
    LEASE_TTL_SECONDS,
    ClaimRecord,
    NullLeaseStore,
    RedisLeaseStore,
    is_expired,
    make_lease_store,
    new_lease_token,
    ownership_key,
)

ORG_ID = "org-test"
RUN_ID = "run-1"
pytestmark = pytest.mark.anyio


@pytest.fixture
def store_factory():
    """Build a fresh RedisLeaseStore backed by an in-process fakeredis client.

    Returns a ``(store, client)`` tuple; the caller closes ``client`` in
    teardown. Kept synchronous so pytest-asyncio strict mode recognises it
    (async fixtures need an explicit marker the suite doesn't carry).
    """
    client = FakeAsyncRedis()
    return RedisLeaseStore(client), client


@pytest.fixture
async def store(store_factory):
    s, client = store_factory
    yield s
    await client.aclose()


# ---------------------------------------------------------------------------
# claim atomicity (TM-026)
# ---------------------------------------------------------------------------


class TestClaim:
    async def test_first_claim_succeeds(self, store):
        result = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        assert result.acquired is True
        assert result.record is not None
        assert result.record.run_id == RUN_ID
        assert result.record.worker_id == "w1"
        assert result.record.worker_version == "v1"
        assert result.record.lease_token  # non-empty bearer

    async def test_second_claim_for_same_run_conflicts(self, store):
        first = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        assert first.acquired

        second = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w2", worker_version="v1")
        assert second.acquired is False
        assert second.record is None
        # The conflict surfaces the current holder for the 409 / metric label.
        assert second.current_holder is not None
        assert second.current_holder.worker_id == "w1"
        assert second.current_holder.lease_token == first.record.lease_token

    async def test_claim_is_org_scoped(self, store):
        """Same run_id in two Orgs is two independent ownership keys."""
        a = await store.claim(run_id=RUN_ID, org_id="org-a", worker_id="w1", worker_version="v1")
        b = await store.claim(run_id=RUN_ID, org_id="org-b", worker_id="w2", worker_version="v1")
        assert a.acquired and b.acquired  # no conflict across Orgs

    async def test_concurrent_claimers_exactly_one_wins(self, store):
        """TM-026: two workers racing on the same run — exactly one acquires."""
        results = await asyncio.gather(
            store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1"),
            store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w2", worker_version="v1"),
            store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w3", worker_version="v1"),
        )
        winners = [r for r in results if r.acquired]
        assert len(winners) == 1
        losers = [r for r in results if not r.acquired]
        assert len(losers) == 2
        # All losers see the same current holder.
        assert all(loser.current_holder.worker_id == winners[0].record.worker_id for loser in losers)

    async def test_get_holder_after_claim(self, store):
        await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        holder = await store.get_holder(org_id=ORG_ID, run_id=RUN_ID)
        assert holder is not None
        assert holder.worker_id == "w1"

    async def test_get_holder_none_when_unclaimed(self, store):
        assert await store.get_holder(org_id=ORG_ID, run_id=RUN_ID) is None


# ---------------------------------------------------------------------------
# renew — token gating (ADR §5.2 "续租只能由当前 lease_token 完成")
# ---------------------------------------------------------------------------


class TestRenew:
    async def test_current_token_can_renew(self, store):
        result = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        assert await store.renew(result.record, org_id=ORG_ID) is True

    async def test_stale_token_cannot_renew(self, store):
        """An old owner's token is rejected once a new owner has claimed."""
        first = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        # Release so a new owner can claim (simulating lease expiry + reclaim).
        assert await store.release(first.record, org_id=ORG_ID) is True
        second = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w2", worker_version="v1")
        assert second.acquired
        # The OLD owner (w1) tries to renew with its now-stale token.
        renewed = await store.renew(first.record, org_id=ORG_ID)
        assert renewed is False
        # The NEW owner (w2) can renew.
        assert await store.renew(second.record, org_id=ORG_ID) is True

    async def test_renew_after_expiry_fails(self, store):
        """When the key has expired (TTL elapsed), renew returns False."""
        result = await store.claim(
            run_id=RUN_ID,
            org_id=ORG_ID,
            worker_id="w1",
            worker_version="v1",
            ttl_seconds=1,
        )
        # fakeredis honours EX; flush expiry by advancing time isn't supported
        # in-process, so we delete to simulate the post-TTL state.
        client = store._client  # noqa: SLF001 — test-only
        await client.delete(ownership_key(org_id=ORG_ID, run_id=RUN_ID))
        assert await store.renew(result.record, org_id=ORG_ID) is False


# ---------------------------------------------------------------------------
# release — token gating
# ---------------------------------------------------------------------------


class TestRelease:
    async def test_current_token_can_release(self, store):
        result = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        assert await store.release(result.record, org_id=ORG_ID) is True
        assert await store.get_holder(org_id=ORG_ID, run_id=RUN_ID) is None

    async def test_stale_token_cannot_release(self, store):
        first = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        await store.release(first.record, org_id=ORG_ID)
        second = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w2", worker_version="v1")
        assert second.acquired  # the new owner (w2) now holds the lease
        # Old owner's token can no longer release (new owner holds it).
        assert await store.release(first.record, org_id=ORG_ID) is False
        # The run is still owned by w2.
        holder = await store.get_holder(org_id=ORG_ID, run_id=RUN_ID)
        assert holder is not None
        assert holder.worker_id == "w2"

    async def test_release_unclaimed_is_false(self, store):
        bogus = ClaimRecord(
            run_id=RUN_ID,
            worker_id="ghost",
            lease_token="nope",
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            worker_version="v1",
            claimed_at=datetime.now(UTC),
        )
        assert await store.release(bogus, org_id=ORG_ID) is False


# ---------------------------------------------------------------------------
# lease expiry / reclaim (the TM-026 / PR-072 reclaim primitive)
# ---------------------------------------------------------------------------


class TestExpiryReclaim:
    async def test_is_expired_true_past_window(self):
        record = ClaimRecord(
            run_id=RUN_ID,
            worker_id="w1",
            lease_token="t",
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            worker_version="v1",
            claimed_at=datetime.now(UTC) - timedelta(seconds=31),
        )
        assert is_expired(record) is True

    async def test_is_expired_false_within_window(self):
        record = ClaimRecord(
            run_id=RUN_ID,
            worker_id="w1",
            lease_token="t",
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=10),
            worker_version="v1",
            claimed_at=datetime.now(UTC),
        )
        assert is_expired(record) is False

    async def test_reclaim_after_release(self, store):
        """After the owner releases, another worker can claim (PR-072 reclaim basis)."""
        first = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        await store.release(first.record, org_id=ORG_ID)
        second = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w2", worker_version="v1")
        assert second.acquired
        assert second.record.worker_id == "w2"


# ---------------------------------------------------------------------------
# ClaimRecord serialization round-trip
# ---------------------------------------------------------------------------


class TestClaimRecordSerialization:
    def test_roundtrip(self):
        now = datetime.now(UTC)
        record = ClaimRecord(
            run_id=RUN_ID,
            worker_id="w1",
            lease_token="abc",
            lease_expires_at=now + timedelta(seconds=30),
            worker_version="v1",
            claimed_at=now,
        )
        restored = ClaimRecord.from_redis_value(record.to_redis_value())
        assert restored == record

    def test_from_none(self):
        assert ClaimRecord.from_redis_value(None) is None
        assert ClaimRecord.from_redis_value(b"") is None


# ---------------------------------------------------------------------------
# NullLeaseStore (dev / single-replica safety net)
# ---------------------------------------------------------------------------


class TestNullLeaseStore:
    async def test_claim_always_succeeds(self):
        store = NullLeaseStore()
        result = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        assert result.acquired is True
        assert result.record.worker_id == "w1"

    async def test_second_claim_also_succeeds_no_conflict(self):
        """No Redis ⇒ no coordination; both 'win' (single-worker assumption)."""
        store = NullLeaseStore()
        a = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        b = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w2", worker_version="v1")
        assert a.acquired and b.acquired

    async def test_renew_release_noop(self):
        store = NullLeaseStore()
        result = await store.claim(run_id=RUN_ID, org_id=ORG_ID, worker_id="w1", worker_version="v1")
        assert await store.renew(result.record, org_id=ORG_ID) is True
        assert await store.release(result.record, org_id=ORG_ID) is True


# ---------------------------------------------------------------------------
# make_lease_store factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_none_url_returns_null_store(self):
        assert isinstance(make_lease_store(None), NullLeaseStore)
        assert isinstance(make_lease_store(""), NullLeaseStore)

    def test_redis_url_returns_redis_store(self):
        store = make_lease_store("redis://localhost:6379/0")
        assert isinstance(store, RedisLeaseStore)


# ---------------------------------------------------------------------------
# constants sanity
# ---------------------------------------------------------------------------


class TestConstants:
    def test_heartbeat_less_than_half_ttl(self):
        """2× heartbeat < TTL guarantees a renew opportunity before expiry."""
        assert 2 * HEARTBEAT_INTERVAL_SECONDS < LEASE_TTL_SECONDS

    def test_lease_token_is_unique(self):
        assert new_lease_token() != new_lease_token()


# ---------------------------------------------------------------------------
# Real-Redis integration (skipped unless REDIS_URL is reachable)
# ---------------------------------------------------------------------------


@pytest.mark.real_redis
class TestRealRedisIntegration:
    """End-to-end against a live Redis (gnex-redis at localhost:6379).

    Skipped automatically when no Redis is reachable, so the default suite stays
    hermetic (fakeredis above is the authoritative correctness gate).
    """

    @pytest.fixture(autouse=True)
    def _require_redis(self):
        import os

        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        from redis.asyncio import Redis

        async def _check():
            client = Redis.from_url(url)
            try:
                return await client.ping()
            except Exception:  # noqa: BLE001
                return False
            finally:
                await client.aclose()

        if not asyncio.run(_check()):
            pytest.skip("no live Redis at REDIS_URL — run gnex-redis to enable")
        self._url = url

    async def test_real_claim_renew_release(self):
        from redis.asyncio import Redis

        client = Redis.from_url(self._url)
        store = RedisLeaseStore(client)
        try:
            key = ownership_key(org_id=ORG_ID, run_id="real-1")
            await client.delete(key)  # clean slate
            result = await store.claim(run_id="real-1", org_id=ORG_ID, worker_id="real-w1", worker_version="v1")
            assert result.acquired
            assert await store.renew(result.record, org_id=ORG_ID) is True
            assert await store.release(result.record, org_id=ORG_ID) is True
            assert await store.get_holder(org_id=ORG_ID, run_id="real-1") is None
        finally:
            await client.aclose()
