"""In-memory run registry with optional persistent RunStore backing."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from deerflow.utils.time import now_iso as _now_iso

from .ownership import NullLeaseStore as _NullLeaseStoreType
from .schemas import DisconnectMode, RunStatus
from .transitions import (
    assert_run_transition as _assert_run_transition,
)
from .transitions import (
    is_terminal_run_status,
)

if TYPE_CHECKING:
    from deerflow.runtime.runs.store.base import RunStore

#: PR-077: async callback that publishes a Redis cancel-notify (acceleration).
#: Signature: ``(run_id, action) -> None``. Errors are swallowed by the caller
#: (the notify is best-effort; the PG intent is the durable source of truth).
CancelNotifier = Callable[[str, str], Awaitable[None]]

logger = logging.getLogger(__name__)

_RETRYABLE_SQLITE_MESSAGES = (
    "database is locked",
    "database table is locked",
    "database is busy",
)

_RETRYABLE_SQLITE_ERROR_CODES = {
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_LOCKED,
}


def _is_retryable_persistence_error(exc: BaseException) -> bool:
    """Return True for transient SQLite persistence failures.

    SQLite lock contention normally surfaces through either sqlite3 exceptions
    or SQLAlchemy wrappers.  The short bounded retry here protects run status
    finalization from transient writer pressure without hiding permanent
    failures forever.
    """

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        message = str(current).lower()
        if any(fragment in message for fragment in _RETRYABLE_SQLITE_MESSAGES):
            return True
        if isinstance(current, (sqlite3.OperationalError, sqlite3.DatabaseError)):
            error_code = getattr(current, "sqlite_errorcode", None)
            if error_code in _RETRYABLE_SQLITE_ERROR_CODES:
                return True
        for chained in (getattr(current, "orig", None), current.__cause__, current.__context__):
            if isinstance(chained, BaseException):
                pending.append(chained)
    return False


@dataclass(frozen=True)
class PersistenceRetryPolicy:
    """Bounded retry policy for short run-store writes."""

    max_attempts: int = 5
    initial_delay: float = 0.05
    max_delay: float = 1.0
    backoff_factor: float = 2.0


@dataclass
class RunRecord:
    """Mutable record for a single run."""

    run_id: str
    thread_id: str
    assistant_id: str | None
    status: RunStatus
    on_disconnect: DisconnectMode
    multitask_strategy: str = "reject"
    metadata: dict = field(default_factory=dict)
    kwargs: dict = field(default_factory=dict)
    user_id: str | None = None
    # Tenant boundary stamped on the persisted run row (PR-024). Threaded
    # explicitly through ``_store_put_payload`` so retry writes remain
    # tenant-scoped even if the contextvar is gone on the retrying task.
    org_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    abort_action: str = "interrupt"
    error: str | None = None
    model_name: str | None = None
    store_only: bool = False
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    llm_call_count: int = 0
    lead_agent_tokens: int = 0
    subagent_tokens: int = 0
    middleware_tokens: int = 0
    # Per-model token breakdown
    token_usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    message_count: int = 0
    last_ai_message: str | None = None
    first_human_message: str | None = None
    # ReleaseRef pin (PR-056 / ADR-0004 §6 step 7). Frozen at creation; the
    # store payload writes them insert-only (never updated). ``legacy_unpinned``
    # defaults true so a run is legacy until start_run pins it.
    release_package_id: str | None = None
    release_version_id: str | None = None
    release_channel: str | None = None
    release_digest: str | None = None
    legacy_unpinned: bool = True
    # PR-070 CAS token. Mirrors runs.row_version; bumped in lockstep with each
    # status transition so _persist_status can pass the pre-transition value as
    # expected_row_version and detect a concurrent writer. Defaults to 1; a
    # record hydrated from a store row picks up the persisted value.
    row_version: int = 1


class RunManager:
    """In-memory run registry with optional persistent RunStore backing.

    All mutations are protected by an asyncio lock. When a ``store`` is
    provided, serializable metadata is also persisted to the store so
    that run history survives process restarts.
    """

    def __init__(
        self,
        store: RunStore | None = None,
        *,
        persistence_retry_policy: PersistenceRetryPolicy | None = None,
        cancel_notifier: CancelNotifier | None = None,
    ) -> None:
        self._runs: dict[str, RunRecord] = {}
        # Secondary index: thread_id -> insertion-ordered run_id set (a dict is
        # used as an ordered set), maintained in lockstep with ``_runs`` so
        # per-thread queries avoid O(total in-memory runs) full scans while
        # preserving ``_runs`` iteration order (see ``_thread_records_locked``).
        self._runs_by_thread: dict[str, dict[str, None]] = {}
        self._lock = asyncio.Lock()
        self._store = store
        self._persistence_retry_policy = persistence_retry_policy or PersistenceRetryPolicy()
        # PR-077: optional async callback that publishes a Redis cancel-notify
        # when a cancel intent is persisted. ``None`` in dev / single-replica /
        # Redis-unavailable — the heartbeat PG poll is the durable fallback.
        self._cancel_notifier = cancel_notifier

    def _index_run_locked(self, record: RunRecord) -> None:
        """Register *record* in the thread index. Caller must hold ``self._lock``."""
        self._runs_by_thread.setdefault(record.thread_id, {})[record.run_id] = None

    def _unindex_run_locked(self, run_id: str, thread_id: str) -> None:
        """Drop *run_id* from the thread index. Caller must hold ``self._lock``."""
        bucket = self._runs_by_thread.get(thread_id)
        if bucket is not None:
            bucket.pop(run_id, None)
            if not bucket:
                self._runs_by_thread.pop(thread_id, None)

    def _thread_records_locked(self, thread_id: str) -> list[RunRecord]:
        """Return live in-memory records for *thread_id*. Caller must hold ``self._lock``.

        Uses the ``_runs_by_thread`` index for O(runs-in-thread) lookup instead of
        scanning every in-memory run. Correctness rests on the index and ``_runs``
        being mutated in lockstep under ``self._lock`` (no ``await`` between the two
        writes), so any holder of the lock sees them agree. The ``self._runs.get``
        filter is defense-in-depth, not reconciliation: it drops a stale id still in
        the index but already gone from ``_runs``, yet it cannot recover a run that is
        in ``_runs`` but missing from the index (such a run would be silently
        omitted). It guards only that one direction, should a future refactor ever
        break the lockstep invariant.
        """
        run_ids = self._runs_by_thread.get(thread_id)
        if not run_ids:
            return []
        return [record for run_id in run_ids if (record := self._runs.get(run_id)) is not None]

    @staticmethod
    def _store_put_payload(record: RunRecord, *, error: str | None = None) -> dict[str, Any]:
        payload = {
            "thread_id": record.thread_id,
            "assistant_id": record.assistant_id,
            "status": record.status.value,
            "multitask_strategy": record.multitask_strategy,
            "metadata": record.metadata or {},
            "kwargs": record.kwargs or {},
            "error": error if error is not None else record.error,
            "created_at": record.created_at,
            "model_name": record.model_name,
        }
        if record.user_id is not None:
            payload["user_id"] = record.user_id
        if record.org_id is not None:
            payload["org_id"] = record.org_id
        # ReleaseRef pin — insert-only (frozen at creation, ADR §6 step 9).
        # ``RunRepository.put`` only applies these on the INSERT branch; the
        # UPDATE branch (status/token follow-ups) never overwrites them, so a
        # later promote/rollback cannot mutate what a run already executed.
        if record.release_package_id is not None:
            payload["release_package_id"] = record.release_package_id
        if record.release_version_id is not None:
            payload["release_version_id"] = record.release_version_id
        if record.release_channel is not None:
            payload["release_channel"] = record.release_channel
        if record.release_digest is not None:
            payload["release_digest"] = record.release_digest
        # legacy_unpinned always carries (defaults True); the INSERT branch
        # writes it, the UPDATE branch leaves it (an unpinned run stays unpinned).
        payload["legacy_unpinned"] = record.legacy_unpinned
        return payload

    async def _call_store_with_retry(
        self,
        operation_name: str,
        run_id: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run a short store operation with bounded retries for SQLite pressure."""
        policy = self._persistence_retry_policy
        attempt = 1
        delay = policy.initial_delay
        while True:
            try:
                return await operation()
            except Exception as exc:
                retryable = _is_retryable_persistence_error(exc)
                if attempt >= policy.max_attempts or not retryable:
                    raise
                logger.warning(
                    "Transient persistence failure during %s for run %s (attempt %d/%d); retrying",
                    operation_name,
                    run_id,
                    attempt,
                    policy.max_attempts,
                    exc_info=True,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                delay = min(policy.max_delay, delay * policy.backoff_factor if delay else policy.initial_delay)
                attempt += 1

    async def _persist_snapshot_to_store(self, run_id: str, payload: dict[str, Any]) -> bool:
        """Best-effort persist a previously captured run snapshot."""
        if self._store is None:
            return True
        try:
            await self._call_store_with_retry(
                "put",
                run_id,
                lambda: self._store.put(run_id, **payload),
            )
            return True
        except Exception:
            logger.warning("Failed to persist run %s to store", run_id, exc_info=True)
            return False

    async def _persist_new_run_to_store(self, record: RunRecord) -> None:
        """Persist a newly created run record to the backing store.

        Initial run creation is part of the run visibility boundary: callers
        should not observe a run in memory unless its backing store row exists.
        Unlike follow-up status/model updates, failures are propagated so the
        caller can treat creation as failed. Rollback is the caller's
        responsibility after inserting the record into ``_runs``.
        """
        if self._store is None:
            return
        await self._call_store_with_retry(
            "put",
            record.run_id,
            lambda: self._store.put(record.run_id, **self._store_put_payload(record)),
        )

    async def _persist_to_store(self, record: RunRecord, *, error: str | None = None) -> bool:
        """Best-effort persist run record to backing store."""
        return await self._persist_snapshot_to_store(
            record.run_id,
            self._store_put_payload(record, error=error),
        )

    async def _persist_status(
        self,
        record: RunRecord,
        status: RunStatus,
        *,
        error: str | None = None,
        expected_row_version: int | None = None,
    ) -> bool:
        """Best-effort persist a status transition to the backing store.

        With ``expected_row_version`` (PR-070), the store write is a
        compare-and-set: a stale expected version means a concurrent writer
        landed a terminal transition first, and ``update_status`` returns
        ``False``. In that case we do NOT fall back to the snapshot recovery
        path (that would overwrite the winner's terminal state, violating
        terminal immutability) — we log and report the CAS miss so the caller
        can observe it. The caller has already mutated the in-memory record;
        the store is the source of truth across workers.
        """
        if self._store is None:
            return True
        row_recovery_payload = self._store_put_payload(record, error=error)
        try:
            updated = await self._call_store_with_retry(
                "update_status",
                record.run_id,
                lambda: self._store.update_status(record.run_id, status.value, error=error, expected_row_version=expected_row_version),
            )
            if updated is False:
                if expected_row_version is not None:
                    # Distinguish a genuine CAS miss (row exists, but a concurrent
                    # writer already bumped row_version — do NOT overwrite the
                    # winner's terminal state) from a missing row (initial
                    # persistence was lost — recreate it for durability). A
                    # missing row is a durability gap, not a race loss.
                    existing = await self._store.get(record.run_id)
                    if existing is None:
                        return await self._persist_snapshot_to_store(record.run_id, row_recovery_payload)
                    logger.warning(
                        "Run %s status CAS miss (expected row_version=%s); a concurrent writer won",
                        record.run_id,
                        expected_row_version,
                    )
                    return False
                return await self._persist_snapshot_to_store(record.run_id, row_recovery_payload)
            if expected_row_version is not None:
                # CAS succeeded and the store bumped row_version; mirror it in
                # the in-memory record so the next transition's expected value
                # is correct.
                record.row_version = expected_row_version + 1
            return True
        except Exception:
            logger.warning("Failed to persist status update for run %s", record.run_id, exc_info=True)
            return False

    @staticmethod
    def _record_from_store(row: dict[str, Any]) -> RunRecord:
        """Build a read-only runtime record from a serialized store row.

        NULL status/on_disconnect columns (e.g. from rows written before those
        columns were added) default to ``pending`` and ``cancel`` respectively.
        """
        return RunRecord(
            run_id=row["run_id"],
            thread_id=row["thread_id"],
            assistant_id=row.get("assistant_id"),
            status=RunStatus(row.get("status") or RunStatus.pending.value),
            on_disconnect=DisconnectMode(row.get("on_disconnect") or DisconnectMode.cancel.value),
            multitask_strategy=row.get("multitask_strategy") or "reject",
            metadata=row.get("metadata") or {},
            kwargs=row.get("kwargs") or {},
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at") or "",
            user_id=row.get("user_id"),
            org_id=row.get("org_id"),
            error=row.get("error"),
            model_name=row.get("model_name"),
            store_only=True,
            total_input_tokens=row.get("total_input_tokens") or 0,
            total_output_tokens=row.get("total_output_tokens") or 0,
            total_tokens=row.get("total_tokens") or 0,
            llm_call_count=row.get("llm_call_count") or 0,
            lead_agent_tokens=row.get("lead_agent_tokens") or 0,
            subagent_tokens=row.get("subagent_tokens") or 0,
            middleware_tokens=row.get("middleware_tokens") or 0,
            token_usage_by_model=row.get("token_usage_by_model") or {},
            message_count=row.get("message_count") or 0,
            last_ai_message=row.get("last_ai_message"),
            first_human_message=row.get("first_human_message"),
            # ReleaseRef pin (PR-056). Rows written before 0016 / before
            # enforcement lack the columns → default to legacy-unpinned, which
            # is the safe admission posture for a run with no frozen identity.
            release_package_id=row.get("release_package_id"),
            release_version_id=row.get("release_version_id"),
            release_channel=row.get("release_channel"),
            release_digest=row.get("release_digest"),
            legacy_unpinned=row.get("legacy_unpinned", True),
            # PR-070 CAS token. Rows written before migration 0017 lack the
            # column → default to 1 (the CAS baseline), same as the column's
            # server_default.
            row_version=row.get("row_version", 1),
        )

    async def update_run_completion(self, run_id: str, **kwargs) -> None:
        """Persist token usage and completion data to the backing store.

        With ``expected_row_version`` (PR-070), the store write is a CAS: a
        concurrent writer that already moved the row to a terminal state
        (e.g. ``interrupted`` from cancel) causes this write to return ``False``
        — the completion data is then dropped rather than clobbering the winner
        (PR-077 §16.72: cancel-vs-completion single winner).
        """
        expected_row_version = kwargs.get("expected_row_version")
        row_recovery_payload: dict[str, Any] | None = None
        async with self._lock:
            record = self._runs.get(run_id)
            if record is not None:
                for key, value in kwargs.items():
                    if key == "status":
                        continue
                    if key == "expected_row_version":
                        continue
                    if hasattr(record, key) and value is not None:
                        setattr(record, key, value)
                record.updated_at = _now_iso()
                row_recovery_payload = self._store_put_payload(record, error=kwargs.get("error"))
        if self._store is None:
            return
        try:
            updated = await self._call_store_with_retry(
                "update_run_completion",
                run_id,
                lambda: self._store.update_run_completion(run_id, **kwargs),
            )
            if updated is False:
                # CAS mismatch (expected_row_version set) means a concurrent
                # writer won the terminal state — do NOT recreate the row or
                # retry; the winner's status stands.
                if expected_row_version is not None:
                    logger.info(
                        "Run %s completion CAS mismatch (expected row_version=%s) — a concurrent writer (likely cancel) won; completion data dropped",
                        run_id,
                        expected_row_version,
                    )
                    return
                if row_recovery_payload is None:
                    logger.warning("Failed to recreate missing run %s for completion persistence", run_id)
                    return
                if not await self._persist_snapshot_to_store(run_id, row_recovery_payload):
                    return
                recovered = await self._call_store_with_retry(
                    "update_run_completion",
                    run_id,
                    lambda: self._store.update_run_completion(run_id, **kwargs),
                )
                if recovered is False:
                    logger.warning("Run completion update for %s affected no rows after row recreation", run_id)
        except Exception:
            logger.warning("Failed to persist run completion for %s", run_id, exc_info=True)

    async def update_run_progress(self, run_id: str, **kwargs) -> None:
        """Persist a running token/message snapshot without changing status."""
        should_persist = True
        async with self._lock:
            record = self._runs.get(run_id)
            if record is not None:
                should_persist = record.status == RunStatus.running
            if record is not None and should_persist:
                for key, value in kwargs.items():
                    if hasattr(record, key) and value is not None:
                        setattr(record, key, value)
                record.updated_at = _now_iso()
        if should_persist and self._store is not None:
            try:
                await self._store.update_run_progress(run_id, **kwargs)
            except Exception:
                logger.warning("Failed to persist run progress for %s", run_id, exc_info=True)

    async def create(
        self,
        thread_id: str,
        assistant_id: str | None = None,
        *,
        on_disconnect: DisconnectMode = DisconnectMode.cancel,
        metadata: dict | None = None,
        kwargs: dict | None = None,
        multitask_strategy: str = "reject",
        user_id: str | None = None,
        org_id: str | None = None,
    ) -> RunRecord:
        """Create a new pending run and register it."""
        run_id = str(uuid.uuid4())
        now = _now_iso()
        record = RunRecord(
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=assistant_id,
            status=RunStatus.pending,
            on_disconnect=on_disconnect,
            multitask_strategy=multitask_strategy,
            metadata=metadata or {},
            kwargs=kwargs or {},
            user_id=user_id,
            org_id=org_id,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._runs[run_id] = record
            self._index_run_locked(record)
            persisted = False
            try:
                await self._persist_new_run_to_store(record)
                persisted = True
            except Exception:
                logger.warning("Failed to persist run %s; rolled back in-memory record", run_id, exc_info=True)
                raise
            finally:
                # Also covers cancellation, which bypasses ``except Exception``.
                if not persisted:
                    self._runs.pop(run_id, None)
                    self._unindex_run_locked(run_id, record.thread_id)
        logger.info("Run created: run_id=%s thread_id=%s", run_id, thread_id)
        return record

    async def get(self, run_id: str, *, user_id: str | None = None) -> RunRecord | None:
        """Return a run record by ID, or ``None``.

        Args:
            run_id: The run ID to look up.
            user_id: Optional user ID for permission filtering when hydrating from store.
        """
        async with self._lock:
            record = self._runs.get(run_id)
        if record is not None:
            return record
        if self._store is None:
            return None
        try:
            row = await self._store.get(run_id, user_id=user_id)
        except Exception:
            logger.warning("Failed to hydrate run %s from store", run_id, exc_info=True)
            return None
        # Re-check after store await: a concurrent create() may have inserted the
        # in-memory record while the store call was in flight.
        async with self._lock:
            record = self._runs.get(run_id)
        if record is not None:
            return record
        if row is None:
            return None
        try:
            return self._record_from_store(row)
        except Exception:
            logger.warning("Failed to map store row for run %s", run_id, exc_info=True)
            return None

    async def aget(self, run_id: str, *, user_id: str | None = None) -> RunRecord | None:
        """Return a run record by ID, checking the persistent store as fallback.

        Alias for :meth:`get` for backward compatibility.
        """
        return await self.get(run_id, user_id=user_id)

    async def list_by_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        org_id: str | None = None,
        limit: int = 100,
    ) -> list[RunRecord]:
        """Return runs for a given thread, newest first, at most ``limit`` records.

        In-memory runs take precedence only when the same ``run_id`` exists in both
        memory and the backing store. The merged result is then sorted newest-first
        by ``created_at`` and trimmed to ``limit`` (default 100).

        Args:
            thread_id: The thread ID to filter by.
            user_id: Optional user ID for permission filtering when hydrating from store.
            org_id: Optional org ID for tenant filtering when hydrating from store
                (``None`` bypasses the org filter, used by startup recovery).
            limit: Maximum number of runs to return.
        """
        async with self._lock:
            memory_records = self._thread_records_locked(thread_id)
        if self._store is None:
            return sorted(memory_records, key=lambda r: r.created_at, reverse=True)[:limit]
        records_by_id = {record.run_id: record for record in memory_records}
        store_limit = max(0, limit - len(memory_records))
        try:
            rows = await self._store.list_by_thread(thread_id, user_id=user_id, org_id=org_id, limit=store_limit)
        except Exception:
            logger.warning("Failed to hydrate runs for thread %s from store", thread_id, exc_info=True)
            return sorted(memory_records, key=lambda r: r.created_at, reverse=True)[:limit]
        for row in rows:
            run_id = row.get("run_id")
            if run_id and run_id not in records_by_id:
                try:
                    records_by_id[run_id] = self._record_from_store(row)
                except Exception:
                    logger.warning("Failed to map store row for run %s", run_id, exc_info=True)
        return sorted(records_by_id.values(), key=lambda record: record.created_at, reverse=True)[:limit]

    async def set_status(self, run_id: str, status: RunStatus, *, error: str | None = None) -> None:
        """Transition a run to a new status."""
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                logger.warning("set_status called for unknown run %s", run_id)
                return
            # PR-070: enforce the state machine. assert_run_transition raises
            # IllegalRunTransitionError on an illegal edge (incl. any transition
            # out of a terminal state). No-op self-transitions are also rejected
            # by the guard — the CAS path, not the guard, handles the write.
            _assert_run_transition(record.status.value, status.value)
            # Capture the pre-transition row_version so _persist_status can CAS
            # against it (a concurrent terminal completion must not be silently
            # overwritten — TM-027).
            expected_row_version = record.row_version
            record.status = status
            record.updated_at = _now_iso()
            if error is not None:
                record.error = error
        await self._persist_status(record, status, error=error, expected_row_version=expected_row_version)
        # PR-063: bump §4.3 runs_status_total on every transition. The counter
        # is the §6 SLO numerator/denominator source; fail-open (metrics never
        # break the run). Also recompute worker_active (pending+running count)
        # so the gauge tracks reality without a separate scraper task.
        from deerflow.observability.metrics import inc_runs_status, set_worker_active

        inc_runs_status(run_status=status.value)
        async with self._lock:
            active = sum(1 for r in self._runs.values() if r.status in (RunStatus.pending, RunStatus.running))
        set_worker_active(active)
        logger.info("Run %s -> %s", run_id, status.value)

    async def _persist_model_name(self, run_id: str, model_name: str | None) -> None:
        """Best-effort persist model_name update to the backing store."""
        if self._store is None:
            return
        try:
            await self._call_store_with_retry(
                "update_model_name",
                run_id,
                lambda: self._store.update_model_name(run_id, model_name),
            )
        except Exception:
            logger.warning("Failed to persist model_name update for run %s", run_id, exc_info=True)

    async def update_model_name(self, run_id: str, model_name: str | None) -> None:
        """Update the model name for a run."""
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                logger.warning("update_model_name called for unknown run %s", run_id)
                return
            record.model_name = model_name
            record.updated_at = _now_iso()
        await self._persist_model_name(run_id, model_name)
        logger.info("Run %s model_name=%s", run_id, model_name)

    async def cancel(self, run_id: str, *, action: str = "interrupt") -> bool:
        """Request cancellation of a run (ADR-0006 §5.4).

        Args:
            run_id: The run ID to cancel.
            action: "interrupt" keeps checkpoint, "rollback" reverts to pre-run state.

        Two paths:

        * **Local fast-path** — the run is live in this process (``self._runs``).
          Sets the abort event with the action reason, cancels the asyncio task,
          and persists the ``interrupted`` terminal status via CAS (PR-070). Also
          persists the cancel intent in PG (defence-in-depth: if the local CAS
          loses to a concurrent completion, the intent is still durable).
        * **Cross-replica path** (PR-077) — the run is NOT in this process (the
          cancel HTTP landed on a different replica than the lease-holding
          worker). Persists the cancel intent in PG (the durable source of
          truth) + publishes a Redis notify (acceleration). The owner polls the
          intent in its heartbeat loop and stops the run. Returns ``True`` once
          the intent is durable (the owner will see it within one heartbeat
          interval); ``False`` if the run is terminal / unknown.

        Returns ``True`` if cancellation was initiated **or** the run was already
        interrupted (idempotent — a second cancel is a no-op success).
        Returns ``False`` only when the run is unknown / has reached a terminal
        state other than interrupted (completed, failed, etc.).
        """
        from deerflow.observability.metrics import inc_run_cancel

        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                # Cross-replica: persist the durable intent; the lease-holding
                # worker's heartbeat poll will see it and set its local
                # abort_event. The cancel-vs-completion race is arbitrated by
                # the terminal-status CAS the owner performs when it stops.
                if self._store is None:
                    return False
                persisted = await self._store.request_cancel(run_id, action=action)
                if persisted:
                    await self._publish_cancel_notify(run_id, action=action)
                    inc_run_cancel()
                    logger.info("Run %s cancel intent persisted (cross-replica, action=%s)", run_id, action)
                    return True
                # Already cancelled (idempotent) / terminal / unknown.
                # Distinguish idempotent-already-cancelled from terminal/unknown
                # by re-reading the intent: if cancel_requested is already true,
                # treat as idempotent success (§5.4 — a repeat cancel is a no-op).
                existing = await self._store.get_cancel_intent(run_id)
                if existing is not None and existing.get("cancel_requested"):
                    return True
                return False
            if record.status == RunStatus.interrupted:
                return True  # idempotent — already cancelled on this worker
            if record.status not in (RunStatus.pending, RunStatus.running):
                return False
            # PR-070: the early-return above already guarantees this is a legal
            # cancel edge (pending|running → interrupted); assert explicitly so a
            # future vocabulary change can't silently bypass the state machine.
            _assert_run_transition(record.status.value, RunStatus.interrupted.value)
            # Capture the pre-cancel row_version for the CAS persist.
            expected_row_version = record.row_version
            record.abort_action = action
            record.abort_event.set()
            if record.task is not None and not record.task.done():
                record.task.cancel()
            record.status = RunStatus.interrupted
            record.updated_at = _now_iso()
        await self._persist_status(record, RunStatus.interrupted, expected_row_version=expected_row_version)
        # PR-077: persist the durable intent too (defence-in-depth — if the
        # local terminal CAS loses to a concurrent completion, the intent is
        # still durable for observability / a reconciler audit).
        if self._store is not None:
            try:
                await self._store.request_cancel(run_id, action=action)
            except Exception:  # noqa: BLE001 — intent persist is best-effort
                logger.debug("Run %s cancel-intent persist failed (terminal CAS already won)", run_id, exc_info=True)
        await self._publish_cancel_notify(run_id, action=action)
        logger.info("Run %s cancelled (action=%s)", run_id, action)
        # PR-063: bump §4.3 run_cancel_total when a real cancellation is
        # initiated (not the idempotent already-interrupted path above).
        inc_run_cancel()
        return True

    async def _publish_cancel_notify(self, run_id: str, *, action: str) -> None:
        """Best-effort Redis cancel-notify (ADR-0006 §5.4 bullet 2 acceleration).

        The PG intent (``request_cancel``) is the durable source of truth; this
        notify only accelerates delivery. If no notifier is wired (dev /
        single-replica / Redis unavailable) this is a no-op — the owner's
        heartbeat PG poll is the fallback (§5.4 bullet 4).
        """
        notifier = getattr(self, "_cancel_notifier", None)
        if notifier is None:
            return
        try:
            await notifier(run_id, action)
        except Exception:  # noqa: BLE001 — notify is best-effort
            logger.debug("Run %s cancel notify failed (PG intent is still durable)", run_id, exc_info=True)

    async def create_or_reject(
        self,
        thread_id: str,
        assistant_id: str | None = None,
        *,
        on_disconnect: DisconnectMode = DisconnectMode.cancel,
        metadata: dict | None = None,
        kwargs: dict | None = None,
        multitask_strategy: str = "reject",
        model_name: str | None = None,
        user_id: str | None = None,
        org_id: str | None = None,
        release_package_id: str | None = None,
        release_version_id: str | None = None,
        release_channel: str | None = None,
        release_digest: str | None = None,
        legacy_unpinned: bool = True,
    ) -> RunRecord:
        """Atomically check for inflight runs and create a new one.

        For ``reject`` strategy, raises ``ConflictError`` if thread
        already has a pending/running run.  For ``interrupt``/``rollback``,
        cancels inflight runs before creating.

        This method holds the lock across both the check and the insert,
        eliminating the TOCTOU race in separate ``has_inflight`` + ``create``.
        """
        run_id = str(uuid.uuid4())
        now = _now_iso()

        _supported_strategies = ("reject", "interrupt", "rollback")
        interrupted_records: list[RunRecord] = []

        async with self._lock:
            if multitask_strategy not in _supported_strategies:
                raise UnsupportedStrategyError(f"Multitask strategy '{multitask_strategy}' is not yet supported. Supported strategies: {', '.join(_supported_strategies)}")

            inflight = [r for r in self._thread_records_locked(thread_id) if r.status in (RunStatus.pending, RunStatus.running)]

            if multitask_strategy == "reject" and inflight:
                raise ConflictError(f"Thread {thread_id} already has an active run")

            if multitask_strategy in ("interrupt", "rollback") and inflight:
                logger.info(
                    "Preparing to cancel %d inflight run(s) on thread %s (strategy=%s)",
                    len(inflight),
                    thread_id,
                    multitask_strategy,
                )

            record = RunRecord(
                run_id=run_id,
                thread_id=thread_id,
                assistant_id=assistant_id,
                status=RunStatus.pending,
                on_disconnect=on_disconnect,
                multitask_strategy=multitask_strategy,
                metadata=metadata or {},
                kwargs=kwargs or {},
                user_id=user_id,
                org_id=org_id,
                created_at=now,
                updated_at=now,
                model_name=model_name,
                release_package_id=release_package_id,
                release_version_id=release_version_id,
                release_channel=release_channel,
                release_digest=release_digest,
                legacy_unpinned=legacy_unpinned,
            )
            self._runs[run_id] = record
            self._index_run_locked(record)
            persisted = False
            try:
                await self._persist_new_run_to_store(record)
                persisted = True
            except Exception:
                logger.warning("Failed to persist run %s; rolled back in-memory record", run_id, exc_info=True)
                raise
            finally:
                # Also covers cancellation, which bypasses ``except Exception``.
                if not persisted:
                    self._runs.pop(run_id, None)
                    self._unindex_run_locked(run_id, record.thread_id)

            if multitask_strategy in ("interrupt", "rollback") and inflight:
                for r in inflight:
                    r.abort_action = multitask_strategy
                    r.abort_event.set()
                    if r.task is not None and not r.task.done():
                        r.task.cancel()
                    r.status = RunStatus.interrupted
                    r.updated_at = now
                    interrupted_records.append(r)

        for interrupted_record in interrupted_records:
            await self._persist_status(interrupted_record, RunStatus.interrupted)
        logger.info("Run created: run_id=%s thread_id=%s", run_id, thread_id)
        # PR-063: bump §4.3 runs_created_total. The §6.2 run-create SLO
        # denominator counts runs that pass auth/authz/basic validation, i.e.
        # runs that actually got created — this is that count.
        from deerflow.observability.metrics import inc_runs_created

        inc_runs_created()
        return record

    async def reconcile_orphaned_inflight_runs(
        self,
        *,
        error: str,
        before: str | None = None,
        lease_store: Any = None,
        run_event_store: Any = None,
    ) -> list[RunRecord]:
        """Converge non-terminal runs whose owner is gone to a safe terminal.

        PR-072 refines the pre-existing blanket-``error`` sweep into a
        lease-aware, PG-first reconciler (Track G):

        * **PG terminal first** (ADR-0006 §5.3): a row already in a terminal
          status is skipped — PG terminal state is authoritative and must never
          be revived by the reconciler (TM-029).
        * **lease-aware** (PR-071): when a ``lease_store`` is supplied, a run
          whose lease holder is live (``not is_expired``) is skipped — it is
          actively owned on another worker and must not be raced
          (``skipped_live_elsewhere``). A run with no holder or an expired
          lease is an orphan.
        * **local-process check** (NullLeaseStore / single-worker path): a run
          with a live in-memory task is skipped (``skipped_live``).
        * **safe terminal, no replay** (TM-028): an orphan is driven to
          ``error`` via the PR-070 CAS (``expected_row_version``). The
          reconciler NEVER retries/replays the run — it only converges the
          ambiguous non-terminal state to an explicit terminal one, emitting a
          ``run.reconcile.result`` event so an operator can decide on any
          manual follow-up (the "人工处理" half of the PR-072 deliverable).
        * **CAS conflict** (``cas_conflict`` outcome): if a concurrent writer
          already moved the row, the CAS misses and the row is left untouched.

        ``list_inflight`` returns rows with ``row_version`` + ``status``, so the
        CAS token and terminal check are available per row.
        """
        if self._store is None:
            return []
        try:
            rows = await self._call_store_with_retry(
                "list_inflight",
                "*",
                lambda: self._store.list_inflight(before=before),
            )
        except Exception:
            logger.warning("Failed to list orphaned inflight runs for reconciliation", exc_info=True)
            return []

        recovered: list[RunRecord] = []
        now = _now_iso()
        # PR-063: §4.3 run_reconcile_backlog — the count of inflight rows the
        # reconciler is iterating. Set once up-front (before the per-row
        # loop) so a dashboard sees the backlog even if the loop is slow.
        from deerflow.observability.metrics import inc_run_reconcile, set_run_reconcile_backlog

        set_run_reconcile_backlog(len(rows))
        for row in rows:
            try:
                record = self._record_from_store(row)
            except Exception:
                logger.warning("Failed to map orphaned run row during reconciliation", exc_info=True)
                inc_run_reconcile(outcome="row_map_failed")
                continue

            # PG terminal first (ADR §5.3 / TM-029): a row that already reached
            # a terminal state is authoritative — never revive it.
            if is_terminal_run_status(record.status.value):
                inc_run_reconcile(outcome="terminal_already_set")
                continue

            # lease-aware: a live holder means the run is owned elsewhere.
            if lease_store is not None and not isinstance(lease_store, _NullLeaseStoreType):
                try:
                    from .ownership import is_expired

                    holder = await lease_store.get_holder(org_id=record.org_id or "", run_id=record.run_id)
                    if holder is not None and not is_expired(holder):
                        inc_run_reconcile(outcome="skipped_live_elsewhere")
                        continue
                    outcome_label = "expired_lease_reclaimed" if holder is not None else "recovered"
                except Exception:
                    # A lease-store error must not block convergence: fall back to
                    # the local-process check + safe terminal. Tag the outcome so
                    # the degradation is observable.
                    logger.warning(
                        "lease store check failed for run %s; falling back to local reclaim",
                        record.run_id,
                        exc_info=True,
                    )
                    outcome_label = "recovered"
            else:
                outcome_label = "recovered"

            # local-process check (NullLeaseStore / single-worker): a run with a
            # live in-memory task is still executing here.
            async with self._lock:
                live_record = self._runs.get(record.run_id)
                if live_record is not None and live_record.status in (RunStatus.pending, RunStatus.running):
                    inc_run_reconcile(outcome="skipped_live")
                    continue

            # Safe terminal via PR-070 CAS. Capture the pre-reclaim row_version
            # so a concurrent writer (the owner, or another reconciler) that
            # already moved the row wins the CAS.
            expected_row_version = record.row_version
            record.status = RunStatus.error
            record.error = error
            record.updated_at = now
            persisted = await self._persist_status(record, RunStatus.error, error=error, expected_row_version=expected_row_version)
            if not persisted:
                # A CAS miss means a concurrent writer won — do NOT overwrite
                # (TM-029). Distinguish a genuine conflict from a missing row
                # (already handled inside _persist_status via snapshot-recreate).
                logger.info(
                    "Reconcile CAS miss for run %s (expected row_version=%s); concurrent writer won",
                    record.run_id,
                    expected_row_version,
                )
                inc_run_reconcile(outcome="cas_conflict")
                continue
            recovered.append(record)
            inc_run_reconcile(outcome=outcome_label)
            # run.reconcile.result event (data-model §12.3): every correction is
            # observable so an operator can decide on manual follow-up.
            if run_event_store is not None:
                try:
                    await run_event_store.put(
                        thread_id=record.thread_id,
                        run_id=record.run_id,
                        event_type="run.reconcile.result",
                        category="system",
                        content={"run_id": record.run_id, "outcome": outcome_label, "reason": error},
                        metadata={},
                    )
                except Exception:
                    logger.debug(
                        "Failed to emit run.reconcile.result event for run %s",
                        record.run_id,
                        exc_info=True,
                    )

        if recovered:
            logger.warning("Recovered %d orphaned inflight run(s) as error", len(recovered))
        return recovered

    async def has_inflight(self, thread_id: str) -> bool:
        """Return ``True`` if *thread_id* has a pending or running run."""
        async with self._lock:
            return any(r.status in (RunStatus.pending, RunStatus.running) for r in self._thread_records_locked(thread_id))

    async def cleanup(self, run_id: str, *, delay: float = 300) -> None:
        """Remove a run record after an optional delay."""
        if delay > 0:
            await asyncio.sleep(delay)
        async with self._lock:
            record = self._runs.pop(run_id, None)
            if record is not None:
                self._unindex_run_locked(run_id, record.thread_id)
        logger.debug("Run record %s cleaned up", run_id)

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Cancel and bounded-await all in-flight runs on process shutdown.

        Chat runs execute in fire-and-forget background ``asyncio`` tasks that
        write checkpoints through a shared checkpointer. On shutdown the
        checkpointer's resources (e.g. the postgres connection pool owned by the
        gateway's ``AsyncExitStack``) are torn down; if a run task is still
        mid-graph at that point, langgraph's
        ``AsyncPregelLoop._checkpointer_put_after_previous`` runs its
        ``finally: await checkpointer.aput(...)`` against the closed pool. Because
        that put runs in a langgraph-internal task (not on ``run_agent``'s call
        stack), the resulting ``psycopg_pool.PoolClosed`` is not catchable by the
        worker and surfaces as an unhandled exception during ``asyncio.run()``
        shutdown (bytedance/deer-flow issue #3373).

        Draining in-flight runs *before* the checkpointer is closed lets each
        run that settles within ``timeout`` flush its final checkpoint while
        resources are still open. Only runs that do **not** settle on their own
        are marked ``interrupted`` — a run that completes (e.g. ``success``)
        during the drain keeps its real terminal status instead of being
        blanket-overwritten. The whole drain, including the trailing status
        persistence, is bounded by ``timeout`` so a run stuck in cleanup (or a
        slow store under DB pressure) cannot hang worker shutdown — the
        precondition for the signal-reentrancy deadlock guarded by
        ``app.gateway.app._SHUTDOWN_HOOK_TIMEOUT_SECONDS``. Runs still active
        after ``timeout`` are logged and may still race teardown.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        async with self._lock:
            inflight = [record for record in self._runs.values() if record.status in (RunStatus.pending, RunStatus.running) and record.task is not None and not record.task.done()]
            for record in inflight:
                record.abort_action = "interrupt"
                record.abort_event.set()
                record.task.cancel()  # type: ignore[union-attr]  # filtered above
                # Status is decided AFTER the drain (below), not here: a run that
                # completes on its own during the drain must keep its real status.

        if not inflight:
            return

        tasks = [record.task for record in inflight]
        _, pending = await asyncio.wait(tasks, timeout=timeout)

        # Only mark/persist ``interrupted`` for runs that did not settle on their
        # own (still pending after the timeout, or ended cancelled). A run that
        # finished normally during the drain keeps the status it set for itself.
        to_persist: list[RunRecord] = []
        async with self._lock:
            for record in inflight:
                task = record.task
                if task not in pending and not task.cancelled():
                    # Completed on its own — retrieve any surfaced exception so it
                    # is not reported as "never retrieved", and keep its status.
                    task.exception()  # type: ignore[union-attr]  # done & not cancelled
                    continue
                if record.status in (RunStatus.pending, RunStatus.running):
                    record.status = RunStatus.interrupted
                    record.updated_at = _now_iso()
                to_persist.append(record)

        # Bound the trailing status persistence within the remaining budget so a
        # slow store (``_call_store_with_retry`` can back off under DB pressure)
        # cannot push shutdown past ``timeout``.
        if to_persist:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning("Run drain budget exhausted before persisting %d interrupted run(s) on shutdown", len(to_persist))
            else:
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*(self._persist_status(record, RunStatus.interrupted) for record in to_persist), return_exceptions=True),
                        timeout=remaining,
                    )
                except TimeoutError:
                    logger.warning("Run drain status persistence exceeded the %.1fs budget; %d record(s) may not be persisted", timeout, len(to_persist))
                else:
                    # ``_persist_status`` is best-effort: it catches and logs its
                    # own failures, returning ``False``. Inspect the aggregate so a
                    # partial failure is surfaced at shutdown level (with the
                    # run_id) instead of being silently swallowed by the gather.
                    for record, result in zip(to_persist, results):
                        if isinstance(result, Exception):
                            logger.warning("Unexpected error persisting interrupted status for run %s during shutdown: %r", record.run_id, result)
                        elif result is False:
                            logger.warning("Could not persist interrupted status for run %s during shutdown", record.run_id)

        if pending:
            logger.warning("Run drain exceeded %.1fs on shutdown; %d run task(s) still active and may race checkpointer teardown", timeout, len(pending))
        logger.info("Drained %d in-flight run(s) on shutdown (%d settled within %.1fs)", len(inflight), len(inflight) - len(pending), timeout)


class ConflictError(Exception):
    """Raised when multitask_strategy=reject and thread has inflight runs."""


class UnsupportedStrategyError(Exception):
    """Raised when a multitask_strategy value is not yet implemented."""
