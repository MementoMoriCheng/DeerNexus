"""Run ownership / lease primitives — Redis coordination layer (PR-071, Track G).

Implements the four primitives ``pr-split-guide.md`` §12 names for PR-071: a
Redis key scheme, a lease token, heartbeat (renew), and an atomic claim. The
lease is **Redis-only** (ADR-0006 §3.1): PostgreSQL remains the authoritative
source for Run / terminal state (PR-070's ``runs.row_version`` CAS enforces
terminal immutability); Redis carries only the run → owner mapping with a TTL.
A worker that holds a run's lease is its owner and is the only one allowed to
drive that run's state transitions.

Claim / renew / release follow ADR-0006 §5.2-5.3:

* **claim** — ``SET key value NX EX ttl``: atomic; exactly one of two concurrent
  contenders wins (TM-026 multi-Worker same-Run mitigation). A lost claim
  surfaces the current holder for observability / a 409 to the caller.
* **renew** — a Lua compare-and-set: only the current ``lease_token`` may
  extend the lease. An expired old owner must NOT clobber a new owner
  (ADR §5.2: "续租只能由当前 lease_token 完成").
* **release** — a Lua compare-and-del: the owner drops the key on completion
  (after the terminal CAS has landed in PG). Release failure cannot revive a
  committed terminal run — PG terminal state is authoritative (ADR §5.3).

The module is optional: when ``production.redis.url`` is unset (dev / local /
single-replica), no lease layer exists and ``NullLeaseStore`` makes claim a
no-op success, preserving today's single-worker behaviour as a backward-
compatibility safety net (mirrors ``production.agent_release.enforce``'s
single-switch posture).
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Lease time-to-live (seconds). A worker must renew within this window or the
#: lease expires and another worker may reclaim. Picked so that a single missed
#: heartbeat (HEARTBEAT_INTERVAL_SECONDS) still leaves the current owner within
#: the TTL — 2× heartbeat < TTL guarantees one renew opportunity per interval.
LEASE_TTL_SECONDS: int = 30

#: How often the owner renews (extends) the lease while a run is executing.
#: Must be < LEASE_TTL_SECONDS / 2 so a missed renewal does not expire the lease
#: before the next attempt.
HEARTBEAT_INTERVAL_SECONDS: float = 10.0

#: Redis key namespace prefix (Org-scoped per data-model.md §"Redis ... 具备 Org
#: namespace"). The full key is ``OWNERSHIP_KEY_PREFIX:{org_id}:{run_id}``.
OWNERSHIP_KEY_PREFIX: str = "deerflow:run:ownership"


def ownership_key(*, org_id: str, run_id: str) -> str:
    """Build the Redis key for a run's ownership record."""
    if not org_id:
        raise ValueError("org_id is required for the ownership key")
    if not run_id:
        raise ValueError("run_id is required for the ownership key")
    return f"{OWNERSHIP_KEY_PREFIX}:{org_id}:{run_id}"


def new_lease_token() -> str:
    """Generate an unguessable per-claim lease token (renew/release bearer)."""
    return secrets.token_urlsafe(24)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ClaimRecord:
    """The ownership record stored under the run's Redis key (ADR-0006 §5.2).

    Six fields exactly as the ADR prescribes: ``run_id, worker_id, lease_token,
    lease_expires_at, worker_version, claimed_at``.
    """

    run_id: str
    worker_id: str
    lease_token: str
    lease_expires_at: datetime
    worker_version: str
    claimed_at: datetime

    def to_redis_value(self) -> str:
        """Serialize for storage (ISO datetimes, stable keys)."""
        d = asdict(self)
        d["lease_expires_at"] = self.lease_expires_at.isoformat()
        d["claimed_at"] = self.claimed_at.isoformat()
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_redis_value(cls, raw: str | bytes | None) -> ClaimRecord | None:
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        d = json.loads(raw)
        return cls(
            run_id=d["run_id"],
            worker_id=d["worker_id"],
            lease_token=d["lease_token"],
            lease_expires_at=datetime.fromisoformat(d["lease_expires_at"]),
            worker_version=d.get("worker_version", ""),
            claimed_at=datetime.fromisoformat(d["claimed_at"]),
        )


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of a claim attempt.

    ``record`` is set on success; on conflict ``current_holder`` carries the
    winning owner (for a 409 / observability) and ``record`` is ``None``.
    """

    record: ClaimRecord | None
    current_holder: ClaimRecord | None = None

    @property
    def acquired(self) -> bool:
        return self.record is not None


def is_expired(record: ClaimRecord, *, now: datetime | None = None) -> bool:
    """Local check: has the lease's TTL window elapsed?

    Note this is a *local* judgement on the stored ``lease_expires_at``; the
    authoritative expiry is Redis dropping the key at TTL. A Reconciler (PR-072)
    uses this to decide which non-terminal runs to reclaim.
    """
    return (now or _now()) >= record.lease_expires_at


# Lua: renew only if the stored lease_token matches. Returns 1 on success, 0
# otherwise. Updates BOTH the Redis key TTL AND the ``lease_expires_at`` field
# in the stored JSON value — a bare ``SET key current EX ttl`` would leave the
# embedded ``lease_expires_at`` frozen at the claim-time value, so a Reconciler
# (PR-072) reading the stale field via ``is_expired`` would reclaim a lease
# whose Redis key is still very much alive (Bug: long runs killed at ~TTL).
# ARGV: [1]=lease_token, [2]=ttl_seconds, [3]=new lease_expires_at (ISO string).
_RENEW_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
local t = cjson.decode(current)
if t['lease_token'] ~= ARGV[1] then return 0 end
t['lease_expires_at'] = ARGV[3]
local updated = cjson.encode(t)
redis.call('SET', KEYS[1], updated, 'EX', tonumber(ARGV[2]))
return 1
"""

# Lua: release only if the stored lease_token matches. Returns the DEL result.
_RELEASE_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
local t = cjson.decode(current)
if t['lease_token'] ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class LeaseStore(Protocol):
    """Abstract ownership/lease store. Implementations: Redis (prod), Null (dev), Fake (tests).

    ``org_id`` is passed to renew/release because it is part of the Redis key
    (Org namespace) but deliberately NOT stored in the claim record value (the
    record is the ADR §5.2 six-field shape; org is a keying concern).
    """

    async def claim(
        self,
        *,
        run_id: str,
        org_id: str,
        worker_id: str,
        worker_version: str,
        ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> ClaimResult:
        """Atomically claim ownership of ``run_id``.

        On success returns ``ClaimResult(record=..., acquired=True)``. If the
        run is already owned (key exists) returns
        ``ClaimResult(current_holder=..., acquired=False)`` — exactly one of two
        concurrent contenders wins (TM-026).
        """
        ...

    async def renew(self, record: ClaimRecord, *, org_id: str, ttl_seconds: int = LEASE_TTL_SECONDS) -> bool:
        """Extend the lease. Only the current ``record.lease_token`` may renew.

        Returns ``False`` if the token no longer matches (a new owner won or the
        key expired) — the caller must stop driving the run.
        """
        ...

    async def release(self, record: ClaimRecord, *, org_id: str) -> bool:
        """Drop the ownership key. Only the current token holder may release.

        Returns ``False`` if the token no longer matches (new owner) — safe to
        ignore on the completion path since PG terminal state is authoritative.
        """
        ...

    async def get_holder(self, *, org_id: str, run_id: str) -> ClaimRecord | None:
        """Read the current owner without claiming (observability / Reconciler)."""
        ...

    async def close(self) -> None:
        """Release any underlying connection."""
        ...


class NullLeaseStore:
    """No-op lease store for single-worker / dev (no Redis configured).

    ``claim`` always succeeds (returns a synthetic record); renew/release are
    no-ops. This preserves today's behaviour when ``production.redis.url`` is
    unset — ownership is a production-only coordination layer.
    """

    async def claim(
        self,
        *,
        run_id: str,
        org_id: str,
        worker_id: str,
        worker_version: str,
        ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> ClaimResult:
        now = _now()
        return ClaimResult(
            record=ClaimRecord(
                run_id=run_id,
                worker_id=worker_id,
                lease_token=new_lease_token(),
                lease_expires_at=datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=UTC),
                worker_version=worker_version,
                claimed_at=now,
            )
        )

    async def renew(self, record: ClaimRecord, *, org_id: str, ttl_seconds: int = LEASE_TTL_SECONDS) -> bool:
        return True

    async def release(self, record: ClaimRecord, *, org_id: str) -> bool:
        return True

    async def get_holder(self, *, org_id: str, run_id: str) -> ClaimRecord | None:
        return None

    async def close(self) -> None:
        return None


class RedisLeaseStore:
    """Redis-backed ownership/lease store (production).

    Uses ``SET ... NX EX`` for an atomic claim and Lua compare-and-set scripts
    for token-gated renew/release. The store holds a ``redis.asyncio.Redis``
    client (or a fakeredis ``FakeAsyncRedis`` in tests); callers pass either in.
    """

    def __init__(self, client: Any) -> None:
        # Typed as Any to accept both redis.asyncio.Redis and fakeredis
        # FakeAsyncRedis without a hard dev-dependency on fakeredis here.
        self._client = client

    async def claim(
        self,
        *,
        run_id: str,
        org_id: str,
        worker_id: str,
        worker_version: str,
        ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> ClaimResult:
        now = _now()
        record = ClaimRecord(
            run_id=run_id,
            worker_id=worker_id,
            lease_token=new_lease_token(),
            lease_expires_at=datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=UTC),
            worker_version=worker_version,
            claimed_at=now,
        )
        key = ownership_key(org_id=org_id, run_id=run_id)
        # SET key value NX EX ttl — atomic claim. NX fails (None) if the key
        # already exists, which is the single-winner guarantee.
        acquired = await self._client.set(key, record.to_redis_value(), nx=True, ex=ttl_seconds)
        if acquired:
            return ClaimResult(record=record)
        # Conflict: read the current holder for the caller's 409 / metric label.
        holder = await self.get_holder(org_id=org_id, run_id=run_id)
        return ClaimResult(record=None, current_holder=holder)

    async def renew(self, record: ClaimRecord, *, org_id: str, ttl_seconds: int = LEASE_TTL_SECONDS) -> bool:
        """Extend the lease for ``record`` (which must carry the current token).

        Refreshes both the Redis key TTL and the ``lease_expires_at`` field in
        the stored JSON so a Reconciler's ``is_expired`` check reflects the
        renewed window, not the claim-time snapshot.
        """
        key = ownership_key(org_id=org_id, run_id=record.run_id)
        new_expires = _now() + timedelta(seconds=ttl_seconds)
        result = await self._client.eval(_RENEW_SCRIPT, 1, key, record.lease_token, ttl_seconds, new_expires.isoformat())
        return bool(result)

    async def release(self, record: ClaimRecord, *, org_id: str) -> bool:
        """Drop the ownership key, gated on the record's lease token."""
        key = ownership_key(org_id=org_id, run_id=record.run_id)
        result = await self._client.eval(_RELEASE_SCRIPT, 1, key, record.lease_token)
        return bool(result)

    async def get_holder(self, *, org_id: str, run_id: str) -> ClaimRecord | None:
        key = ownership_key(org_id=org_id, run_id=run_id)
        raw = await self._client.get(key)
        return ClaimRecord.from_redis_value(raw)

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close is not None:
            await close()


def make_lease_store(redis_url: str | None) -> LeaseStore:
    """Build a lease store from a config URL.

    ``None`` / empty → :class:`NullLeaseStore` (dev / single-replica: no
    ownership layer, claim is a no-op success — backward-compat safety net).
    A ``redis://`` / ``rediss://`` / ``redis+socket://`` URL →
    :class:`RedisLeaseStore` over a ``redis.asyncio.Redis`` client.
    """
    if not redis_url:
        return NullLeaseStore()
    # Imported lazily so the redis dependency is only required when Redis is
    # actually configured (dev installs need not pull it transitively at import
    # time; the dep is declared, but this keeps NullLeaseStore importable even
    # in environments where redis is absent at runtime).
    from redis.asyncio import Redis  # type: ignore[import-not-found]

    return RedisLeaseStore(Redis.from_url(redis_url))


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "LEASE_TTL_SECONDS",
    "OWNERSHIP_KEY_PREFIX",
    "ClaimRecord",
    "ClaimResult",
    "LeaseStore",
    "NullLeaseStore",
    "RedisLeaseStore",
    "is_expired",
    "make_lease_store",
    "new_lease_token",
    "ownership_key",
]
