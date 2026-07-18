"""Abstract interface for run metadata storage.

RunManager depends on this interface. Implementations:
- MemoryRunStore: in-memory dict (development, tests)
- RunRepository: SQLAlchemy ORM (sqlite / postgres)

All methods accept an optional ``user_id`` for user isolation, and since
PR-024 an optional ``org_id`` for the hard tenant boundary
(runtime-contracts §5.2, data-model §11.2). When either is ``None``, no
filter on that dimension is applied (single-user / migration / CLI mode).
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Any


class RunStore(abc.ABC):
    @abc.abstractmethod
    async def put(
        self,
        run_id: str,
        *,
        thread_id: str,
        assistant_id: str | None = None,
        user_id: str | None = None,
        org_id: str | None = None,
        model_name: str | None = None,
        status: str = "pending",
        multitask_strategy: str = "reject",
        metadata: dict[str, Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        error: str | None = None,
        created_at: str | None = None,
    ) -> None:
        pass

    @abc.abstractmethod
    async def get(
        self,
        run_id: str,
        *,
        user_id: str | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any] | None:
        pass

    @abc.abstractmethod
    async def list_by_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        org_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        pass

    @abc.abstractmethod
    async def update_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> bool | None:
        """Update a run status.

        Returns ``False`` when the store can prove no row was updated. Older or
        lightweight stores may return ``None`` when they cannot report rowcount.
        """
        pass

    @abc.abstractmethod
    async def delete(self, run_id: str, *, user_id: str | None = None, org_id: str | None = None) -> None:
        pass

    @abc.abstractmethod
    async def update_model_name(
        self,
        run_id: str,
        model_name: str | None,
    ) -> None:
        """Update the model_name field for an existing run."""
        pass

    @abc.abstractmethod
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
    ) -> bool | None:
        """Persist final completion fields.

        Returns ``False`` when the store can prove no row was updated.
        """
        pass

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
        """Persist a best-effort running snapshot without changing run status."""
        return None

    @abc.abstractmethod
    async def list_pending(self, *, before: str | None = None) -> list[dict[str, Any]]:
        pass

    @abc.abstractmethod
    async def list_inflight(self, *, before: str | None = None) -> list[dict[str, Any]]:
        """Return persisted runs that are still ``pending`` or ``running``."""
        pass

    @abc.abstractmethod
    async def aggregate_tokens_by_thread(self, thread_id: str, *, include_active: bool = False) -> dict[str, Any]:
        """Aggregate token usage for completed runs in a thread.

        Returns a dict with keys: total_tokens, total_input_tokens,
        total_output_tokens, total_runs, by_model (model_name → {tokens, runs}),
        by_caller ({lead_agent, subagent, middleware}).
        """
        pass

    @abc.abstractmethod
    async def aggregate_tokens_by_org(
        self,
        org_id: str | None = None,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        include_active: bool = False,
    ) -> dict[str, Any]:
        """Org-level token aggregation (PR-060 Org Console API).

        Same return shape as :meth:`aggregate_tokens_by_thread`, plus a
        time-window filter on ``created_at``. When ``org_id`` is None no
        org filter is applied (migration / CLI / system-admin path).
        """
        pass

    @abc.abstractmethod
    async def aggregate_stats_by_org(
        self,
        org_id: str | None = None,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        """Org-level run-status rollup (PR-060 Org Console API).

        Returns total_runs, runs_by_status, failure_rate,
        recent_runs_24h, recent_failures_24h, window_start, window_end.
        """
        pass

    @abc.abstractmethod
    async def list_runs_by_org(
        self,
        org_id: str | None = None,
        *,
        status: str | None = None,
        model: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: tuple[datetime, str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Org-scoped keyset-paginated listing (PR-060 Org Console API).

        Returns ``(rows, has_more)``. Keyset on ``(created_at DESC, run_id DESC)``.
        """
        pass
