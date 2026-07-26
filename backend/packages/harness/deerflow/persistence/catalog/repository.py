"""Read-side repository for the ``catalog_entries`` discovery index (PR-054).

Pure data-access layer — no audit, no authz. The write path (import / promote
projecting into the Catalog) lands in a follow-up; this module owns only the
``GET /catalog`` read path.

Conventions mirror ``persistence/release/repository.py``:

* Each function opens its own ``AsyncSession`` from the supplied
  ``async_sessionmaker``.
* Reads always filter by ``org_id`` (ADR §8); a missing ``org_id`` is a
  programming error (``ValueError``).
* Cross-Org existence-hiding: a get that misses (wrong Org or absent) returns
  ``None`` so the router emits an identical 404.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.catalog.model import (
    CATALOG_STATUS_ACTIVE,
    CatalogEntryRow,
)


async def list_catalog_entries(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    workspace_id: str | None = None,
    resource_type: str | None = None,
    status: str = CATALOG_STATUS_ACTIVE,
) -> list[CatalogEntryRow]:
    """Return catalog entries in ``org_id``, optionally filtered; newest-synced first.

    ``status`` defaults to ``active`` so archived / disabled rows are hidden
    from the default browse; pass ``status=None``-equivalent by sending
    ``status=""`` to include all (the caller filters client-side). The
    ``workspace_id`` filter is opt-in; when ``None``, entries with NULL
    workspace AND entries with any workspace are returned (cross-workspace
    browse). Pass a specific ``workspace_id`` to scope to that workspace.
    """
    if not org_id:
        raise ValueError("org_id is required for catalog reads")
    async with sf() as session:
        stmt = select(CatalogEntryRow).where(CatalogEntryRow.org_id == org_id)
        if workspace_id is not None:
            stmt = stmt.where(CatalogEntryRow.workspace_id == workspace_id)
        if resource_type is not None:
            stmt = stmt.where(CatalogEntryRow.resource_type == resource_type)
        if status:
            stmt = stmt.where(CatalogEntryRow.status == status)
        stmt = stmt.order_by(CatalogEntryRow.synced_at.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_catalog_entry(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    resource_type: str,
    resource_id: str,
) -> CatalogEntryRow | None:
    """Return the catalog entry for ``(org, resource_type, resource_id)``, or ``None``.

    Cross-Org existence-hiding: a same-resource entry in another Org returns
    ``None`` (router → 404). Does NOT filter by ``status`` — a caller that
    needs the active-only view should branch on the returned ``status``.
    """
    if not org_id:
        raise ValueError("org_id is required for catalog reads")
    async with sf() as session:
        stmt = select(CatalogEntryRow).where(
            CatalogEntryRow.org_id == org_id,
            CatalogEntryRow.resource_type == resource_type,
            CatalogEntryRow.resource_id == resource_id,
        )
        result = await session.execute(stmt)
        return result.scalars().first()
