"""Async stream bridge factory.

Provides an **async context manager** aligned with
:func:`deerflow.runtime.checkpointer.async_provider.make_checkpointer`.

Usage (e.g. FastAPI lifespan)::

    from deerflow.agents.stream_bridge import make_stream_bridge

    async with make_stream_bridge() as bridge:
        app.state.stream_bridge = bridge
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from deerflow.config.app_config import AppConfig
from deerflow.config.stream_bridge_config import get_stream_bridge_config

from .base import StreamBridge

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def make_stream_bridge(app_config: AppConfig | None = None) -> AsyncIterator[StreamBridge]:
    """Async context manager that yields a :class:`StreamBridge`.

    Falls back to :class:`MemoryStreamBridge` when no configuration is
    provided and nothing is set globally.
    """
    if app_config is None:
        config = get_stream_bridge_config()
    else:
        config = app_config.stream_bridge

    if config is None or config.type == "memory":
        from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge

        maxsize = config.queue_maxsize if config is not None else 256
        bridge = MemoryStreamBridge(queue_maxsize=maxsize)
        logger.info("Stream bridge initialised: memory (queue_maxsize=%d)", maxsize)
        try:
            yield bridge
        finally:
            await bridge.close()
        return

    if config.type == "redis":
        # PR-073: cross-replica SSE recovery. The stream bridge lives in Redis
        # (ADR-0006 §4.4) so a subscriber on replica B can read events a worker
        # on replica A produced. Fall back to ``production.redis.url`` when the
        # dedicated ``stream_bridge.redis_url`` is unset, keeping this backend
        # consistent with the lease/reconciler/doctor wiring (all read
        # ``production.redis.url``). With no URL at all we degrade to memory
        # rather than hard-fail boot — single-replica deployments keep working.
        redis_url = config.redis_url
        if not redis_url and app_config is not None:
            redis_url = getattr(getattr(getattr(app_config, "production", None), "redis", None), "url", None)

        if not redis_url:
            logger.warning("Stream bridge type=redis requested but no redis URL configured (stream_bridge.redis_url or production.redis.url); falling back to memory bridge")
            from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge

            bridge = MemoryStreamBridge(queue_maxsize=config.queue_maxsize)
            try:
                yield bridge
            finally:
                await bridge.close()
            return

        from redis.asyncio import Redis

        from deerflow.runtime.stream_bridge.redis import RedisStreamBridge

        # decode_responses=True so entry IDs and field names/values arrive as
        # ``str`` (not ``bytes``) — keeps the bridge's string comparisons and
        # JSON decode simple and matches what the SSE consumer expects.
        client = Redis.from_url(redis_url, decode_responses=True)
        bridge = RedisStreamBridge(client, queue_maxsize=config.queue_maxsize)
        logger.info(
            "Stream bridge initialised: redis (queue_maxsize=%d, cross_replica=True)",
            config.queue_maxsize,
        )
        try:
            yield bridge
        finally:
            await bridge.close()
        return

    raise ValueError(f"Unknown stream bridge type: {config.type!r}")
