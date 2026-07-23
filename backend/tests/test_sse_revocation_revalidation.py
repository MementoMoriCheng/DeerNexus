"""SSE re-validation + revocation-close evidence (PR-037, ADR-0003 §11).

Proves the §11 SSE clause — "已建立 SSE 至少每 60 秒或在发送下一条业务事件前
重新确认主体、Membership 和 Key 状态;发现撤权后关闭流,不继续发送业务事件"
(testing-strategy §17 line 610) — by driving ``sse_consumer`` directly with a
controllable ``authorize`` outcome. The template is
``test_runtime_lifecycle_e2e::test_sse_consumer_disconnect_cancels_inflight_run``:
swap the disconnect signal for a revocation signal.

RUN IDs: ``RUN-030`` series.

Invariants under test:
  - mid-stream revocation → stream emits a ``revoked`` close frame and breaks.
  - the existing ``finally`` cancels the run (abort_event set, interrupted).
  - NO business event is yielded after the revocation (ordering invariant,
    ADR §11 + testing-strategy §17).
  - a non-revoked stream stays open (re-validation passes).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import patch

import pytest

from app.gateway.services import sse_consumer
from deerflow.runtime import (
    DisconnectMode,
    MemoryStreamBridge,
    RunManager,
    RunStatus,
)


class _StubRequest:
    """Minimal Request stand-in: never disconnected."""

    headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


async def _run_sse(bridge, record, run_mgr, *, allow_sequence):
    """Drive ``sse_consumer`` with a controllable authorize outcome.

    ``allow_sequence`` is a list of bools: each re-validation call pops the
    next value. The last value is reused once exhausted. This lets a test
    say "allow, allow, then deny" to simulate a mid-stream revocation.
    """
    from app.gateway.authorize import AuthorizeError
    from deerflow.contracts import ErrorCode

    call_count = {"n": 0}

    class _StubAuthorizeService:
        async def authorize(self, ctx, permission, *, force_refresh=False, **kw):
            i = call_count["n"]
            call_count["n"] += 1
            allowed = allow_sequence[i] if i < len(allow_sequence) else allow_sequence[-1]
            if not allowed:
                raise AuthorizeError(ErrorCode.PERMISSION_DENIED, "revoked mid-stream")

    frames = []
    # ``get_authorize_service`` is imported lazily inside ``_sse_revalidate``
    # (``from app.gateway.authorize import get_authorize_service``), so the
    # patch must target the SOURCE module, not ``app.gateway.services``.
    with patch("app.gateway.authorize.get_authorize_service", return_value=_StubAuthorizeService()):
        async for frame in sse_consumer(bridge, record, _StubRequest(), run_mgr):
            frames.append(frame)
    return frames, call_count["n"]


async def _make_running_record(*, user_id="u-sse", org_id="org-sse"):
    run_manager = RunManager()
    record = await run_manager.create("thread-sse", on_disconnect=DisconnectMode.cancel)
    record.user_id = user_id
    record.org_id = org_id
    await run_manager.set_status(record.run_id, RunStatus.running)

    async def _pending_worker() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    record.task = asyncio.create_task(_pending_worker())
    return run_manager, record


# ===========================================================================
# RUN-030a — mid-stream revocation closes the stream
# ===========================================================================


class TestRevocationClosesStream:
    @pytest.mark.anyio
    async def test_revocation_emits_revoked_frame_and_cancels(self):
        bridge = MemoryStreamBridge()
        run_manager, record = await _make_running_record()
        try:
            # Publish two business events; re-validation allows the first,
            # denies before the second (the revocation lands between them).
            await bridge.publish(record.run_id, "message", {"i": 1})
            await bridge.publish(record.run_id, "message", {"i": 2})
            await bridge.publish_end(record.run_id)

            frames, n_checks = await _run_sse(bridge, record, run_manager, allow_sequence=[True, False])

            # The revoked frame must be present.
            assert any("event: revoked" in f for f in frames), f"no revoked frame in {frames}"
            # The run was cancelled (revocation reuses the disconnect path).
            assert record.abort_event.is_set()
            assert record.status == RunStatus.interrupted
            # At least two re-validation calls happened (allow then deny).
            assert n_checks >= 2
        finally:
            if record.task is not None and not record.task.done():
                record.task.cancel()
                with suppress(asyncio.CancelledError):
                    await record.task

    @pytest.mark.anyio
    async def test_no_business_event_after_revocation(self):
        """Ordering invariant (testing-strategy §17 line 610): nothing leaks past the revocation."""
        bridge = MemoryStreamBridge()
        run_manager, record = await _make_running_record()
        try:
            # Three events; deny before the second so events 2 and 3 must NOT appear.
            await bridge.publish(record.run_id, "message", {"i": 1})
            await bridge.publish(record.run_id, "message", {"i": 2})
            await bridge.publish(record.run_id, "message", {"i": 3})
            await bridge.publish_end(record.run_id)

            frames, _ = await _run_sse(bridge, record, run_manager, allow_sequence=[True, False])

            # Event 1 (allowed) is present; events 2 and 3 are absent.
            assert any('"i": 1' in f for f in frames)
            assert not any('"i": 2' in f for f in frames), f"event 2 leaked: {frames}"
            assert not any('"i": 3' in f for f in frames), f"event 3 leaked: {frames}"
        finally:
            if record.task is not None and not record.task.done():
                record.task.cancel()
                with suppress(asyncio.CancelledError):
                    await record.task


# ===========================================================================
# RUN-030b — non-revoked stream stays open
# ===========================================================================


class TestNonRevokedStreamStaysOpen:
    @pytest.mark.anyio
    async def test_allowed_stream_yields_all_events_then_end(self):
        bridge = MemoryStreamBridge()
        run_manager, record = await _make_running_record()
        try:
            await bridge.publish(record.run_id, "message", {"i": 1})
            await bridge.publish(record.run_id, "message", {"i": 2})
            await bridge.publish_end(record.run_id)

            frames, n_checks = await _run_sse(bridge, record, run_manager, allow_sequence=[True])

            assert any('"i": 1' in f for f in frames)
            assert any('"i": 2' in f for f in frames)
            assert any("event: end" in f for f in frames)
            # No revocation occurred → no revoked frame.
            assert not any("event: revoked" in f for f in frames)
            assert n_checks >= 2
        finally:
            if record.task is not None and not record.task.done():
                record.task.cancel()
                with suppress(asyncio.CancelledError):
                    await record.task


# ===========================================================================
# RUN-030c — no user principal ⇒ re-validation skipped (internal runs)
# ===========================================================================


class TestNoUserPrincipalSkipsRevalidation:
    @pytest.mark.anyio
    async def test_internal_run_without_user_id_not_revocation_checked(self):
        """A run with no user_id (internal/system) has no revocable user auth → skip."""
        bridge = MemoryStreamBridge()
        run_manager, record = await _make_running_record(user_id=None, org_id=None)
        try:
            await bridge.publish(record.run_id, "message", {"i": 1})
            await bridge.publish_end(record.run_id)

            frames, n_checks = await _run_sse(bridge, record, run_manager, allow_sequence=[False])
            # Even though the stub would deny, the re-validation gate is
            # skipped (no user principal), so the event flows through.
            assert any('"i": 1' in f for f in frames)
            assert any("event: end" in f for f in frames)
            assert n_checks == 0
        finally:
            if record.task is not None and not record.task.done():
                record.task.cancel()
                with suppress(asyncio.CancelledError):
                    await record.task
