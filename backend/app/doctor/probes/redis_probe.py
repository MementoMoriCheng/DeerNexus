"""Live Redis connectivity probe for the production doctor (PR-071, Track G).

Implements the ``redis.connectivity`` check: when ``production.redis.url`` is
declared, open a throwaway ``redis.asyncio`` client on that URL, ``PING``, and
verify the server reports stream capability (``XADD`` to a throwaway key). The
probe is only meaningful when Redis is configured — without a URL the check
WARN-skips (dev / single-replica deployments run without the ownership/lease
layer; the NullLeaseStore makes claim a no-op there).

Promoted from the pre-PR-071 DEFERRED_LIVE_CHECKS stub (which named this PR).
No-secret guarantee: the result message carries only the URL's host (or a
label), never the full URL or password.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_CHECK_ID = "redis.connectivity"
_COMPONENT = "redis"


def _host_of(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.hostname or url
    except ValueError:
        return url


async def probe_redis_connectivity(config: AppConfig) -> DoctorCheckResult:
    """Probe the configured Redis for connectivity + stream capability.

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Never raises.
    """
    redis_cfg = getattr(config.production, "redis", None)
    url = getattr(redis_cfg, "url", None) if redis_cfg is not None else None

    if not url:
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.WARN,
            component=_COMPONENT,
            message=(
                "redis.connectivity skipped: production.redis.url is not set. Redis is optional for dev/single-replica (ownership/lease uses NullLeaseStore); production multi-replica deployments require it for run ownership + SSE recovery."
            ),
            remediation=("Set production.redis.url (redis:// or rediss://) for production multi-replica. Safe to leave unset for dev/single-replica."),
            config_source="config.yaml:production.redis",
        )

    host_label = _host_of(url)
    # Lazily import redis so the dependency is only required when a URL is
    # actually configured (the NullLeaseStore path needs no redis at runtime).
    try:
        from redis.asyncio import Redis  # type: ignore[import-not-found]
    except ImportError:
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.FAIL,
            component=_COMPONENT,
            message=("production.redis.url is set but the redis client library is not installed; cannot probe connectivity."),
            remediation="Install the redis package (it is a declared production dependency).",
            config_source="config.yaml:production.redis",
        )

    try:
        client = Redis.from_url(url)
        try:
            pong = await client.ping()
            # Verify stream capability with a throwaway XADD/XDEL round-trip.
            stream_key = "deerflow:doctor:probe"
            entry_id = await client.xadd(stream_key, {"probe": "1"})
            if isinstance(entry_id, bytes):
                entry_id = entry_id.decode("utf-8")
            await client.xdel(stream_key, entry_id)
            await client.delete(stream_key)
        finally:
            await client.aclose()
    except Exception:  # noqa: BLE001 — contain any Redis failure into FAIL
        logger.warning("redis probe could not reach Redis at %s", host_label, exc_info=True)
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.FAIL,
            component=_COMPONENT,
            message=(f"Could not connect to the configured Redis (host={host_label}); the server is unreachable, credentials are invalid, or stream commands are unsupported."),
            remediation=("Check production.redis.url, Redis network reachability, and that the Redis version supports Streams (≥5.0). Re-run doctor after fixing."),
            config_source="config.yaml:production.redis",
        )

    if not pong:
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.FAIL,
            component=_COMPONENT,
            message=f"Redis at {host_label} did not respond to PING.",
            remediation="Verify the Redis server is healthy and reachable.",
            config_source="config.yaml:production.redis",
        )

    return DoctorCheckResult(
        check_id=_CHECK_ID,
        status=DoctorStatus.PASS,
        component=_COMPONENT,
        message=f"Connected to Redis at {host_label} (PING ok; XADD/XDEL stream capability verified).",
        remediation=None,
        config_source="config.yaml:production.redis",
    )


__all__ = ["probe_redis_connectivity"]
