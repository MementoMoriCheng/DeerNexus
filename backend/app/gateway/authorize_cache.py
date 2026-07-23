"""In-memory permission cache for the Authorize Service (PR-031 / PR-037).

ADR-0003 §11 mandates a ≤60-second TTL on cached permission sets, keyed by
``org_id + principal_type + principal_id`` (the cache stores the whole
effective set, not per-permission entries). The active-invalidation half
(``AuthorizeService.invalidate_principal`` / ``invalidate_system_admin``
called from the IAM write path after commit, plus the SSE re-validation
guard in ``app.gateway.services.sse_consumer``) is wired in PR-034/035/037.
This module provides:

* a :class:`PermissionCache` Protocol so a future Redis-backed cross-process
  implementation can drop in without touching the Authorize Service (the
  in-memory cache is single-process; cross-process coherence rides the
  ≤60s TTL fallback, which ADR §11 permits: "主动失效失败时仍不得超过 60 秒");
* an :class:`InMemoryPermissionCache` suitable for single-process deployments
  and tests, honouring the 60-second TTL fallback and the system-admin
  independent namespace (ADR §11).

The cache is intentionally minimal: it stores ``frozenset[str]`` of permission
strings (the output of :func:`app.gateway.authorize.compute_effective_permissions`).
Cache misses and TTL expiries fall through to the Authorize Service, which
re-queries the DB. Because the TTL is the guaranteed correctness bound, a cache
entry that survives past the bound must be treated as stale even if the
``invalidate`` API was never called.
"""

from __future__ import annotations

import time
from typing import Protocol

#: Hard upper bound on cache entry lifetime (ADR-0003 §11: "最大 TTL 60 秒").
#: Producers must not raise this above 60; PR-037 may lower it via config.
DEFAULT_TTL_SECONDS: int = 60

#: Cache-key prefix for Org-scoped principals (users / service_accounts).
_ORG_NAMESPACE = "authz"

#: Cache-key prefix for system-admin principals (ADR §11: "system-admin 权限
#: 使用独立缓存 namespace"). System-admin permissions cross Org boundaries, so
#: the key deliberately omits org_id to keep the namespace isolated.
_SYSTEM_NAMESPACE = "authz:system"


def org_cache_key(*, org_id: str, principal_type: str, principal_id: str) -> str:
    """Build a cache key for an Org-scoped principal.

    ADR §11 requires the key to contain ``org_id + principal_type +
    principal_id``; cross-Org isolation falls out of the key composition
    without extra work.
    """
    return f"{_ORG_NAMESPACE}:{org_id}:{principal_type}:{principal_id}"


def system_cache_key(*, principal_id: str) -> str:
    """Build a cache key for a system-admin principal (independent namespace)."""
    return f"{_SYSTEM_NAMESPACE}:{principal_id}"


class PermissionCache(Protocol):
    """Cache interface consumed by the Authorize Service.

    PR-031 ships :class:`InMemoryPermissionCache`; PR-037 may add a
    Redis-backed implementation that also wires active-invalidation hooks.
    """

    def get(self, key: str) -> frozenset[str] | None:
        """Return the cached permission set if present and unexpired, else ``None``."""
        ...

    def set(self, key: str, value: frozenset[str], *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        """Store ``value`` under ``key`` with at most ``ttl_seconds`` lifetime.

        Implementations MUST clamp ``ttl_seconds`` to ≤60 to honour ADR §11.
        """
        ...

    def invalidate(self, key: str) -> None:
        """Drop a single entry. PR-037 will call this from change hooks."""
        ...

    def clear(self) -> None:
        """Drop every entry. Tests use this between cases for isolation."""
        ...


def _clamp_ttl(ttl_seconds: int) -> int:
    if ttl_seconds < 0:
        return 0
    if ttl_seconds > DEFAULT_TTL_SECONDS:
        return DEFAULT_TTL_SECONDS
    return ttl_seconds


class InMemoryPermissionCache:
    """Process-local ``dict`` + monotonic-clock TTL implementation.

    Suitable for single-process deployments and the test suite. The TTL is
    clamped to :data:`DEFAULT_TTL_SECONDS` on every :meth:`set` so a misconfigured
    caller cannot widen the correctness window beyond ADR §11's bound.
    """

    def __init__(self) -> None:
        # key -> (expires_at_monotonic, value)
        self._entries: dict[str, tuple[float, frozenset[str]]] = {}

    def get(self, key: str) -> frozenset[str] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            # Lazy eviction: stale entries are removed on read.
            self._entries.pop(key, None)
            return None
        return value

    def set(self, key: str, value: frozenset[str], *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        ttl = _clamp_ttl(ttl_seconds)
        self._entries[key] = (time.monotonic() + ttl, value)

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "InMemoryPermissionCache",
    "PermissionCache",
    "org_cache_key",
    "system_cache_key",
]
