"""Redis Streams-backed stream bridge for cross-replica SSE recovery.

The Redis StreamBridge lets a subscriber on replica B read SSE events that a
worker on replica A produced — something the in-process
:class:`~deerflow.runtime.stream_bridge.memory.MemoryStreamBridge` cannot do,
since its event log lives in a single process's memory.

Design (ADR-0006 §4.4 — "SSE StreamBridge" is Redis-resident):

- **One Redis Stream per run**: ``deerflow:run:stream:{run_id}``. ``run_id`` is a
  globally-unique UUID4, so no org dimension is needed (the ``StreamBridge``
  ABC carries only ``run_id``).
- **StreamEvent.id == Redis entry ID** (e.g. ``1719840000000-0``). The SSE
  consumer echoes this back as the ``id:`` field, and the client reconnects with
  ``Last-Event-ID`` carrying the same value. Redis ``XREAD`` IDs are *exclusive*
  ("return entries with ID greater than"), so ``subscribe(last_event_id=X)``
  resumes strictly after ``X`` — Last-Event-ID recovery works across replicas
  with zero translation.
- **Bounded retention via ``MAXLEN ~``**: each business-event ``XADD`` trims the
  stream to approximately *queue_maxsize* entries from the head. This mirrors
  the memory bridge's ``queue_maxsize`` trimming and bounds memory for slow
  clients. A subscriber whose cursor points at a trimmed entry resumes from the
  earliest retained entry (Redis returns entries after the last ID; a trimmed ID
  yields the current head) — equivalent to the memory bridge's ``start_offset``
  fall-back.
- **End signal is a side key, not a stream entry**: ``publish_end`` sets
  ``deerflow:run:stream:{run_id}:ended``. A stream entry would be subject to
  ``MAXLEN`` trimming and could be evicted, losing the terminal signal; a side
  key is never trimmed. ``subscribe`` drains retained events first, then — once a
  poll returns no newer entries AND the ended flag is set — yields
  ``END_SENTINEL``. This preserves ordering (all events precede the end) and
  guarantees the terminal marker is always observable.
- **Non-authoritative (TM-029)**: this bridge only forwards transient SSE
  events. It never touches Run/terminal state — PG remains the sole authority.
  A Redis outage drops live SSE but cannot corrupt a committed terminal run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from .base import END_SENTINEL, HEARTBEAT_SENTINEL, StreamBridge, StreamEvent

logger = logging.getLogger(__name__)

#: Key prefix for per-run streams. Matches the lease namespace convention
#: (``deerflow:run:ownership:*``) so all Redis coordination lives under
#: ``deerflow:run:*``.
STREAM_KEY_PREFIX = "deerflow:run:stream"

#: Suffix for the per-run "ended" flag. A separate key (rather than an ordered
#: ``__end__`` stream entry) is used deliberately: ``MAXLEN`` trimming can evict
#: any entry including a tail marker, which would let the terminal signal be
#: lost. A side key is never trimmed, so a reconnecting subscriber is guaranteed
#: to observe the run's end once it has drained the retained events.
_ENDED_SUFFIX = "ended"

#: Field names inside each stream entry.
_FIELD_EVENT = "event"
_FIELD_DATA = "data"

#: TTL (seconds) applied to stream + ended keys so they self-expire even if
#: the worker's delayed ``cleanup()`` task never runs (process crash / shutdown
#: before the 60s delay). Generous beyond any realistic SSE replay window; the
#: explicit ``cleanup()`` still deletes immediately when it can.
_STREAM_KEY_TTL_SECONDS = 3600


def _stream_key(run_id: str) -> str:
    return f"{STREAM_KEY_PREFIX}:{run_id}"


def _ended_key(run_id: str) -> str:
    return f"{_stream_key(run_id)}:{_ENDED_SUFFIX}"


def _encode_data(data: Any) -> str:
    """Serialise a publish payload to a JSON string for Redis storage.

    ``data`` arrives as a JSON-serialisable object (worker calls
    ``bridge.publish(run_id, event, serialize(chunk, ...))`` with a dict, or a
    literal dict for metadata/error). We store the JSON text so the Redis entry
    is self-describing and round-trips to the same object the memory bridge
    would have retained (the SSE consumer later ``json.dumps`` it again via
    :func:`format_sse`). ``default=str`` matches :func:`format_sse` so non-JSON
    scalars (e.g. datetimes) serialise identically on both sides.
    """
    if isinstance(data, str):
        return data
    return json.dumps(data, default=str, ensure_ascii=False)


def _as_str(value: Any) -> str:
    """Coerce a Redis response value to ``str``.

    redis-py returns ``bytes`` unless the client was built with
    ``decode_responses=True`` (which our factory sets, but a test or a
    hand-constructed client may not). Normalise here so the rest of the bridge
    compares against plain strings.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value if isinstance(value, str) else str(value)


def _field(fields: Any, name: str) -> str:
    """Look up a stream-entry field by name, tolerant of ``bytes`` keys.

    Falls back to the ``bytes`` spelling so the bridge works whether or not the
    Redis client decodes responses.
    """
    if name in fields:
        return fields[name]
    raw = name.encode("utf-8")
    if raw in fields:
        return fields[raw]
    return ""


def _decode_data(raw: str) -> Any:
    """Inverse of :func:`_encode_data`; return the original object when able."""
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # A producer that passed a bare string that is not valid JSON keeps
        # round-tripping as the original string.
        return raw


class RedisStreamBridge(StreamBridge):
    """Cross-replica StreamBridge backed by Redis Streams.

    The bridge is stateless beyond the Redis client: any number of
    ``RedisStreamBridge`` instances pointed at the same Redis (i.e. one per
    replica) see each other's published events, which is what makes SSE recovery
    across replicas work.
    """

    def __init__(self, client: Any, *, queue_maxsize: int = 256) -> None:
        self._client = client
        self._maxlen = max(1, int(queue_maxsize))

    @property
    def cross_replica(self) -> bool:
        return True

    # -- StreamBridge API ------------------------------------------------------

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        key = _stream_key(run_id)
        try:
            await self._client.xadd(
                key,
                {_FIELD_EVENT: event, _FIELD_DATA: _encode_data(data)},
                maxlen=self._maxlen,
                approximate=True,
            )
            # Refresh a TTL on every publish so the stream key self-expires
            # even if the worker's delayed cleanup task never runs (process
            # crash / shutdown before the 60s delay elapses). The TTL is
            # generous (well beyond any realistic SSE replay window) and the
            # explicit cleanup() still deletes immediately when it can.
            await self._client.expire(key, _STREAM_KEY_TTL_SECONDS)
        except Exception:  # noqa: BLE001
            from deerflow.observability.metrics import inc_stream_bridge_redis_error

            inc_stream_bridge_redis_error()
            logger.warning("Redis stream bridge publish failed for run %s", run_id, exc_info=True)

    async def publish_end(self, run_id: str) -> None:
        """Set the per-run ended flag.

        The flag is a side key (``deerflow:run:stream:{run_id}:ended``) rather
        than an ordered stream entry: ``MAXLEN`` trimming can evict any stream
        entry including a tail marker, which would let the terminal signal be
        lost. A side key is never trimmed, so :meth:`subscribe` is guaranteed to
        observe it once the retained events have been drained.
        """
        try:
            ended_key = _ended_key(run_id)
            await self._client.set(_ended_key(run_id), "1")
            await self._client.expire(ended_key, _STREAM_KEY_TTL_SECONDS)
        except Exception:  # noqa: BLE001
            from deerflow.observability.metrics import inc_stream_bridge_redis_error

            inc_stream_bridge_redis_error()
            logger.warning("Redis stream bridge publish_end failed for run %s", run_id, exc_info=True)

    async def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        key = _stream_key(run_id)
        ended_key = _ended_key(run_id)
        # XREAD IDs are exclusive ("entries with ID greater than"). Reconnecting
        # with last_event_id therefore resumes strictly after that entry. A new
        # subscription (no last_event_id) starts from the earliest retained
        # entry via "0".
        cursor: str = last_event_id if last_event_id is not None else "0"

        while True:
            try:
                # Non-blocking XREAD: returns immediately with currently-available
                # entries (or [] when there are none newer than *cursor*). We do
                # NOT use XREAD's ``block`` argument: fakeredis (used in the
                # hermetic test suite) does not honour the block timeout, and the
                # polling loop below sleeps ``heartbeat_interval`` when idle, so
                # the wake-up cadence is identical to a blocking read on a real
                # Redis without depending on server-side timeout behaviour.
                resp = await self._client.xread({key: cursor}, count=100)
            except Exception:  # noqa: BLE001
                from deerflow.observability.metrics import inc_stream_bridge_redis_error

                inc_stream_bridge_redis_error()
                logger.warning("Redis stream bridge xread failed for run %s", run_id, exc_info=True)
                # Surface a heartbeat so a transient Redis blip does not look
                # like stream termination to the client; the loop retries.
                yield HEARTBEAT_SENTINEL
                await asyncio.sleep(heartbeat_interval)
                continue

            if not resp:
                # Drained everything up to the cursor. If the run has signalled
                # its end, deliver END_SENTINEL (after all retained events) so a
                # reconnecting client sees a clean shutdown. Otherwise yield a
                # heartbeat and keep polling for late events.
                try:
                    ended = await self._client.exists(ended_key)
                except Exception:  # noqa: BLE001
                    ended = 0
                if ended:
                    yield END_SENTINEL
                    return
                yield HEARTBEAT_SENTINEL
                await asyncio.sleep(heartbeat_interval)
                continue

            # resp is a list of [key, [(entry_id, fields), ...]] tuples.
            for _resp_key, entries in resp:
                for entry_id, fields in entries:
                    entry_id = _as_str(entry_id)
                    cursor = entry_id  # advance past the last seen entry
                    event = _as_str(_field(fields, _FIELD_EVENT))
                    yield StreamEvent(
                        id=entry_id,
                        event=event,
                        data=_decode_data(_as_str(_field(fields, _FIELD_DATA))),
                    )

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await self._client.delete(_stream_key(run_id), _ended_key(run_id))
        except Exception:  # noqa: BLE001
            logger.debug("Redis stream bridge cleanup failed for run %s", run_id, exc_info=True)

    async def close(self) -> None:
        try:
            aclose = getattr(self._client, "aclose", None)
            if aclose is not None:
                await aclose()
            else:  # pragma: no cover - older redis-py spellings
                close = getattr(self._client, "close", None)
                if close is not None:
                    await close()
        except Exception:  # noqa: BLE001
            logger.debug("Redis stream bridge close failed", exc_info=True)
