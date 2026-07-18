"""SQLAlchemy-backed RunStore implementation.

Each method acquires and releases its own short-lived session.
Run status updates happen from background workers that may live
minutes -- we don't hold connections across long execution.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.contracts import AUTO_ORG, _OrgIdSentinel, resolve_org_id
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.runs.store.base import RunStore
from deerflow.runtime.user_context import AUTO, _AutoSentinel, resolve_user_id
from deerflow.utils.time import coerce_iso


class RunRepository(RunStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _normalize_model_name(model_name: str | None) -> str | None:
        """Normalize model_name for storage: strip whitespace, truncate to 128 chars."""
        if model_name is None:
            return None
        if not isinstance(model_name, str):
            model_name = str(model_name)
        normalized = model_name.strip()
        if len(normalized) > 128:
            normalized = normalized[:128]
        return normalized

    @staticmethod
    def _safe_json(obj: Any) -> Any:
        """Ensure obj is JSON-serializable. Falls back to model_dump() or str()."""
        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, dict):
            return {k: RunRepository._safe_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [RunRepository._safe_json(v) for v in obj]
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump()
            except Exception:
                pass
        if hasattr(obj, "dict"):
            try:
                return obj.dict()
            except Exception:
                pass
        try:
            json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            return str(obj)

    @staticmethod
    def _row_to_dict(row: RunRow) -> dict[str, Any]:
        d = row.to_dict()
        # Remap JSON columns to match RunStore interface
        d["metadata"] = d.pop("metadata_json", {})
        d["kwargs"] = d.pop("kwargs_json", {})
        # Convert datetime to ISO string for consistency with MemoryRunStore.
        # SQLite drops tzinfo on read despite ``DateTime(timezone=True)`` —
        # ``coerce_iso`` normalizes naive datetimes as UTC.
        for key in ("created_at", "updated_at"):
            val = d.get(key)
            if isinstance(val, datetime):
                d[key] = coerce_iso(val)
        return d

    async def put(
        self,
        run_id,
        *,
        thread_id,
        assistant_id=None,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
        model_name: str | None = None,
        status="pending",
        multitask_strategy="reject",
        metadata=None,
        kwargs=None,
        error=None,
        created_at=None,
        follow_up_to_run_id=None,
    ):
        """Insert or update a run row.

        ``RunManager`` retries ``put`` after transient SQLite failures.  Making
        this operation idempotent prevents a successful-but-unacknowledged first
        commit from turning the retry into a primary-key failure.

        ``org_id`` is stamped only on insert (the tenant boundary is immutable
        for an existing run); it is deliberately omitted from ``values`` so the
        update branch never overwrites it. The sentinel is still resolved
        eagerly so a missing tenant context fails closed up front rather than
        silently creating an un-tenant-scoped row on the insert path.
        """
        resolved_user_id = resolve_user_id(user_id, method_name="RunRepository.put")
        resolved_org_id = resolve_org_id(org_id, method_name="RunRepository.put")
        now = datetime.now(UTC)
        created = datetime.fromisoformat(created_at) if created_at else now
        values = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "user_id": resolved_user_id,
            "model_name": self._normalize_model_name(model_name),
            "status": status,
            "multitask_strategy": multitask_strategy,
            "metadata_json": self._safe_json(metadata) or {},
            "kwargs_json": self._safe_json(kwargs) or {},
            "error": error,
            "follow_up_to_run_id": follow_up_to_run_id,
            "updated_at": now,
        }
        async with self._sf() as session:
            row = await session.get(RunRow, run_id)
            if row is None:
                session.add(RunRow(run_id=run_id, org_id=resolved_org_id, created_at=created, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.commit()

    async def get(
        self,
        run_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
    ):
        resolved_user_id = resolve_user_id(user_id, method_name="RunRepository.get")
        resolved_org_id = resolve_org_id(org_id, method_name="RunRepository.get")
        async with self._sf() as session:
            row = await session.get(RunRow, run_id)
            if row is None:
                return None
            if resolved_org_id is not None and row.org_id != resolved_org_id:
                return None
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return None
            return self._row_to_dict(row)

    async def list_by_thread(
        self,
        thread_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
        limit=100,
    ):
        resolved_user_id = resolve_user_id(user_id, method_name="RunRepository.list_by_thread")
        resolved_org_id = resolve_org_id(org_id, method_name="RunRepository.list_by_thread")
        stmt = select(RunRow).where(RunRow.thread_id == thread_id)
        if resolved_org_id is not None:
            stmt = stmt.where(RunRow.org_id == resolved_org_id)
        if resolved_user_id is not None:
            stmt = stmt.where(RunRow.user_id == resolved_user_id)
        stmt = stmt.order_by(RunRow.created_at.desc()).limit(limit)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def update_status(self, run_id, status, *, error=None) -> bool:
        values: dict[str, Any] = {"status": status, "updated_at": datetime.now(UTC)}
        if error is not None:
            values["error"] = error
        async with self._sf() as session:
            result = await session.execute(update(RunRow).where(RunRow.run_id == run_id).values(**values))
            await session.commit()
            return result.rowcount != 0

    async def update_model_name(self, run_id, model_name):
        async with self._sf() as session:
            await session.execute(update(RunRow).where(RunRow.run_id == run_id).values(model_name=self._normalize_model_name(model_name), updated_at=datetime.now(UTC)))
            await session.commit()

    async def delete(
        self,
        run_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
    ):
        resolved_user_id = resolve_user_id(user_id, method_name="RunRepository.delete")
        resolved_org_id = resolve_org_id(org_id, method_name="RunRepository.delete")
        async with self._sf() as session:
            row = await session.get(RunRow, run_id)
            if row is None:
                return
            if resolved_org_id is not None and row.org_id != resolved_org_id:
                return
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return
            await session.delete(row)
            await session.commit()

    async def list_pending(self, *, before=None):
        if before is None:
            before_dt = datetime.now(UTC)
        elif isinstance(before, datetime):
            before_dt = before
        else:
            before_dt = datetime.fromisoformat(before)
        stmt = select(RunRow).where(RunRow.status == "pending", RunRow.created_at <= before_dt).order_by(RunRow.created_at.asc())
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def list_inflight(self, *, before=None):
        """Return persisted active runs for startup recovery."""
        if before is None:
            before_dt = datetime.now(UTC)
        elif isinstance(before, datetime):
            before_dt = before
        else:
            before_dt = datetime.fromisoformat(before)
        stmt = (
            select(RunRow)
            .where(
                RunRow.status.in_(("pending", "running")),
                RunRow.created_at <= before_dt,
            )
            .order_by(RunRow.created_at.asc())
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def update_run_completion(
        self,
        run_id: str,
        *,
        status: str,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        total_tokens: int = 0,
        llm_call_count: int = 0,
        lead_agent_tokens: int = 0,
        subagent_tokens: int = 0,
        middleware_tokens: int = 0,
        token_usage_by_model: dict[str, dict[str, int]] | None = None,
        message_count: int = 0,
        last_ai_message: str | None = None,
        first_human_message: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Update status + token usage + convenience fields on run completion.

        Returns ``False`` when no run row matched the requested ``run_id``.
        """
        values: dict[str, Any] = {
            "status": status,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "llm_call_count": llm_call_count,
            "lead_agent_tokens": lead_agent_tokens,
            "subagent_tokens": subagent_tokens,
            "middleware_tokens": middleware_tokens,
            "token_usage_by_model": self._safe_json(token_usage_by_model) or {},
            "message_count": message_count,
            "updated_at": datetime.now(UTC),
        }
        if last_ai_message is not None:
            values["last_ai_message"] = last_ai_message[:2000]
        if first_human_message is not None:
            values["first_human_message"] = first_human_message[:2000]
        if error is not None:
            values["error"] = error
        async with self._sf() as session:
            result = await session.execute(update(RunRow).where(RunRow.run_id == run_id).values(**values))
            await session.commit()
            return result.rowcount != 0

    async def update_run_progress(
        self,
        run_id: str,
        *,
        total_input_tokens: int | None = None,
        total_output_tokens: int | None = None,
        total_tokens: int | None = None,
        llm_call_count: int | None = None,
        lead_agent_tokens: int | None = None,
        subagent_tokens: int | None = None,
        middleware_tokens: int | None = None,
        token_usage_by_model: dict[str, dict[str, int]] | None = None,
        message_count: int | None = None,
        last_ai_message: str | None = None,
        first_human_message: str | None = None,
    ) -> None:
        """Update token usage + convenience fields while a run is still active."""
        values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
        optional_counters = {
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "llm_call_count": llm_call_count,
            "lead_agent_tokens": lead_agent_tokens,
            "subagent_tokens": subagent_tokens,
            "middleware_tokens": middleware_tokens,
            "message_count": message_count,
        }
        for key, value in optional_counters.items():
            if value is not None:
                values[key] = value
        if token_usage_by_model is not None:
            values["token_usage_by_model"] = self._safe_json(token_usage_by_model) or {}
        if last_ai_message is not None:
            values["last_ai_message"] = last_ai_message[:2000]
        if first_human_message is not None:
            values["first_human_message"] = first_human_message[:2000]
        async with self._sf() as session:
            await session.execute(update(RunRow).where(RunRow.run_id == run_id, RunRow.status == "running").values(**values))
            await session.commit()

    async def aggregate_tokens_by_thread(self, thread_id: str, *, include_active: bool = False) -> dict[str, Any]:
        """Aggregate token usage for a thread.

        ``by_model`` is reduced in Python from each row's ``token_usage_by_model``
        JSON column so subagent / middleware tokens land on the model that
        actually produced them (issue #3645). Rows written before that column
        existed fall back to ``RunRow.model_name`` + ``RunRow.total_tokens``,
        preserving the legacy lead-only behavior instead of dropping the data.

        Headline totals (``total_tokens``, ``total_input_tokens``,
        ``total_output_tokens``) and the ``by_caller`` bucket are summed from
        their own columns and are therefore unaffected by the JSON column being
        empty.
        """
        statuses = ("success", "error", "running") if include_active else ("success", "error")
        _completed = RunRow.status.in_(statuses)
        _thread = RunRow.thread_id == thread_id

        stmt = select(
            RunRow.model_name,
            RunRow.total_tokens,
            RunRow.total_input_tokens,
            RunRow.total_output_tokens,
            RunRow.lead_agent_tokens,
            RunRow.subagent_tokens,
            RunRow.middleware_tokens,
            RunRow.token_usage_by_model,
        ).where(_thread, _completed)

        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()

        total_tokens = total_input = total_output = total_runs = 0
        lead_agent = subagent = middleware = 0
        by_model: dict[str, dict] = {}
        for r in rows:
            total_runs += 1
            total_tokens += r.total_tokens
            total_input += r.total_input_tokens
            total_output += r.total_output_tokens
            lead_agent += r.lead_agent_tokens
            subagent += r.subagent_tokens
            middleware += r.middleware_tokens

            # ``or {}`` covers rows written before ``token_usage_by_model``
            # existed (the column is NULL on a manual ALTER ADD COLUMN without
            # backfill); fresh rows always carry the journal-produced dict.
            usage_by_model = r.token_usage_by_model or {}
            if usage_by_model:
                for model, usage in usage_by_model.items():
                    entry = by_model.setdefault(model, {"tokens": 0, "runs": 0})
                    entry["tokens"] += usage.get("total_tokens", 0)
                    entry["runs"] += 1
            else:
                model = r.model_name or "unknown"
                entry = by_model.setdefault(model, {"tokens": 0, "runs": 0})
                entry["tokens"] += r.total_tokens
                entry["runs"] += 1

        return {
            "total_tokens": total_tokens,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_runs": total_runs,
            "by_model": by_model,
            "by_caller": {
                "lead_agent": lead_agent,
                "subagent": subagent,
                "middleware": middleware,
            },
        }

    # ------------------------------------------------------------------
    # PR-060: Org Console API aggregations. These mirror the thread-scoped
    # methods above but scope by org_id and add an optional time window on
    # created_at (covered by ix_runs_org_status_created). No new index needed.
    # ------------------------------------------------------------------

    async def aggregate_tokens_by_org(
        self,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        include_active: bool = False,
    ) -> dict[str, Any]:
        """Org-level mirror of :meth:`aggregate_tokens_by_thread`.

        Same return shape (``total_tokens`` / ``total_input_tokens`` /
        ``total_output_tokens`` / ``total_runs`` / ``by_model`` / ``by_caller``)
        so the Org Console usage view is a drop-in generalisation of the
        existing thread usage view.
        """
        resolved_org_id = resolve_org_id(org_id, method_name="RunRepository.aggregate_tokens_by_org")
        statuses = ("success", "error", "running") if include_active else ("success", "error")
        conditions: list[Any] = [RunRow.org_id == resolved_org_id, RunRow.status.in_(statuses)]
        if since is not None:
            conditions.append(RunRow.created_at >= since)
        if until is not None:
            conditions.append(RunRow.created_at <= until)

        stmt = select(
            RunRow.model_name,
            RunRow.total_tokens,
            RunRow.total_input_tokens,
            RunRow.total_output_tokens,
            RunRow.lead_agent_tokens,
            RunRow.subagent_tokens,
            RunRow.middleware_tokens,
            RunRow.token_usage_by_model,
        ).where(*conditions)

        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()

        total_tokens = total_input = total_output = total_runs = 0
        lead_agent = subagent = middleware = 0
        by_model: dict[str, dict] = {}
        for r in rows:
            total_runs += 1
            total_tokens += r.total_tokens
            total_input += r.total_input_tokens
            total_output += r.total_output_tokens
            lead_agent += r.lead_agent_tokens
            subagent += r.subagent_tokens
            middleware += r.middleware_tokens

            usage_by_model = r.token_usage_by_model or {}
            if usage_by_model:
                for model, usage in usage_by_model.items():
                    entry = by_model.setdefault(model, {"tokens": 0, "runs": 0})
                    entry["tokens"] += usage.get("total_tokens", 0)
                    entry["runs"] += 1
            else:
                model = r.model_name or "unknown"
                entry = by_model.setdefault(model, {"tokens": 0, "runs": 0})
                entry["tokens"] += r.total_tokens
                entry["runs"] += 1

        return {
            "total_tokens": total_tokens,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_runs": total_runs,
            "by_model": by_model,
            "by_caller": {
                "lead_agent": lead_agent,
                "subagent": subagent,
                "middleware": middleware,
            },
        }

    async def aggregate_stats_by_org(
        self,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        """Org-level run status rollup for the Console stats view.

        Returns: ``total_runs`` (sum of ``runs_by_status``), ``runs_by_status``
        (status → count, windowed), ``failure_rate`` (failures / total in the
        window, 0.0 when empty), ``recent_runs_24h`` and ``recent_failures_24h``
        (independent of the window — a live "right now" signal), and
        ``window_start`` / ``window_end`` echoing the effective bounds.
        """
        resolved_org_id = resolve_org_id(org_id, method_name="RunRepository.aggregate_stats_by_org")
        now = datetime.now(UTC)
        effective_since = since or (now - timedelta(days=7))
        effective_until = until or now
        failure_statuses = ("error", "timeout", "interrupted")

        base_by_status = (
            select(RunRow.status, func.count())
            .where(
                RunRow.org_id == resolved_org_id,
                RunRow.created_at >= effective_since,
                RunRow.created_at <= effective_until,
            )
            .group_by(RunRow.status)
        )

        cutoff_24h = now - timedelta(hours=24)
        recent_runs_stmt = (
            select(func.count())
            .select_from(RunRow)
            .where(
                RunRow.org_id == resolved_org_id,
                RunRow.created_at >= cutoff_24h,
            )
        )
        recent_failures_stmt = (
            select(func.count())
            .select_from(RunRow)
            .where(
                RunRow.org_id == resolved_org_id,
                RunRow.status.in_(failure_statuses),
                RunRow.created_at >= cutoff_24h,
            )
        )

        async with self._sf() as session:
            by_status_rows = (await session.execute(base_by_status)).all()
            recent_runs_24h = (await session.execute(recent_runs_stmt)).scalar_one()
            recent_failures_24h = (await session.execute(recent_failures_stmt)).scalar_one()

        runs_by_status = {status: count for status, count in by_status_rows}
        total_runs = sum(runs_by_status.values())
        failures_in_window = sum(count for status, count in runs_by_status.items() if status in failure_statuses)
        failure_rate = (failures_in_window / total_runs) if total_runs else 0.0
        return {
            "total_runs": total_runs,
            "runs_by_status": runs_by_status,
            "failure_rate": failure_rate,
            "recent_runs_24h": int(recent_runs_24h or 0),
            "recent_failures_24h": int(recent_failures_24h or 0),
            "window_start": effective_since,
            "window_end": effective_until,
        }

    async def list_runs_by_org(
        self,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
        *,
        status: str | None = None,
        model: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: tuple[datetime, str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Org-scoped keyset-paginated listing for the Console runs view.

        Keyset on ``(created_at DESC, run_id DESC)`` so pagination is stable
        under concurrent inserts. Returns ``(rows, has_more)`` where rows carry
        the same dict shape as :meth:`list_by_thread`.
        """
        resolved_org_id = resolve_org_id(org_id, method_name="RunRepository.list_runs_by_org")
        conditions: list[Any] = [RunRow.org_id == resolved_org_id]
        if status is not None:
            conditions.append(RunRow.status == status)
        if model is not None:
            conditions.append(RunRow.model_name == model)
        if since is not None:
            conditions.append(RunRow.created_at >= since)
        if until is not None:
            conditions.append(RunRow.created_at <= until)
        if cursor is not None:
            cursor_at, cursor_id = cursor
            # Strictly-below the cursor row on the (created_at DESC, run_id DESC)
            # ordering — either an older created_at, or the same created_at
            # with a smaller run_id.
            conditions.append(
                or_(
                    RunRow.created_at < cursor_at,
                    and_(RunRow.created_at == cursor_at, RunRow.run_id < cursor_id),
                )
            )

        stmt = (
            select(RunRow)
            .where(*conditions)
            .order_by(
                RunRow.created_at.desc(),
                RunRow.run_id.desc(),
            )
            .limit(limit + 1)
        )

        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = [self._row_to_dict(r) for r in result.scalars()]

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        return rows, has_more
