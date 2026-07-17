"""Abstract interface for thread metadata storage.

Implementations:
- ThreadMetaRepository: SQL-backed (sqlite / postgres via SQLAlchemy)
- MemoryThreadMetaStore: wraps LangGraph BaseStore (memory mode)

All mutating and querying methods accept a ``user_id`` parameter with
three-state semantics (see :mod:`deerflow.runtime.user_context`):

- ``AUTO`` (default): resolve from the request-scoped contextvar.
- Explicit ``str``: use the provided value verbatim.
- Explicit ``None``: bypass owner filtering (migration/CLI only).

Since PR-024, methods additionally accept an ``org_id`` parameter with the
same three-state semantics (see :data:`deerflow.contracts.AUTO_ORG`).
``org_id`` is the hard tenant boundary (runtime-contracts §5.2,
data-model §11.2): when resolved it is stamped on new rows and applied as a
``WHERE`` predicate on reads/mutations alongside the existing ``user_id``
filter (defense in depth; removing the ``user_id`` branch is the Contract
phase, PR-025D).
"""

from __future__ import annotations

import abc
from typing import Any

from deerflow.contracts import AUTO_ORG, _OrgIdSentinel
from deerflow.runtime.user_context import AUTO, _AutoSentinel


class InvalidMetadataFilterError(ValueError):
    """Raised when all client-supplied metadata filter keys are rejected."""


class ThreadMetaStore(abc.ABC):
    @abc.abstractmethod
    async def create(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
        display_name: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        pass

    @abc.abstractmethod
    async def get(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
    ) -> dict | None:
        pass

    @abc.abstractmethod
    async def search(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
    ) -> list[dict[str, Any]]:
        pass

    @abc.abstractmethod
    async def update_display_name(
        self,
        thread_id: str,
        display_name: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
    ) -> None:
        pass

    @abc.abstractmethod
    async def update_status(
        self,
        thread_id: str,
        status: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
    ) -> None:
        pass

    @abc.abstractmethod
    async def update_metadata(
        self,
        thread_id: str,
        metadata: dict,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
    ) -> None:
        """Merge ``metadata`` into the thread's metadata field.

        Existing keys are overwritten by the new values; keys absent from
        ``metadata`` are preserved. No-op if the thread does not exist
        or the owner check fails.
        """
        pass

    @abc.abstractmethod
    async def update_owner(
        self,
        thread_id: str,
        owner_user_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
    ) -> None:
        """Move a thread metadata row to a new owner.

        Intended for trusted internal repair/migration paths. No-op if the
        row does not exist or the caller fails the owner check.
        """
        pass

    @abc.abstractmethod
    async def check_access(
        self,
        thread_id: str,
        user_id: str,
        *,
        require_existing: bool = False,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
    ) -> bool:
        """Check if ``user_id`` has access to ``thread_id``."""
        pass

    @abc.abstractmethod
    async def delete(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        org_id: str | None | _OrgIdSentinel = AUTO_ORG,
    ) -> None:
        pass

