"""Repository tests for the catalog discovery-index read path (PR-054).

Covers :mod:`deerflow.persistence.catalog.repository` against an isolated
SQLite: list filtering (org / workspace / resource_type / status defaults),
get existence-hiding, and cross-Org isolation. The write path lands in a
follow-up, so tests seed rows directly via the ORM.

Catalog IDs: ``ART-1200`` series (repository layer).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.catalog import (
    CATALOG_STATUS_ACTIVE,
    CATALOG_STATUS_ARCHIVED,
    CATALOG_STATUS_DISABLED,
    RESOURCE_TYPE_AGENT,
    RESOURCE_TYPE_SKILL,
    SOURCE_DATABASE,
    CatalogEntryRow,
    get_catalog_entry,
    list_catalog_entries,
)

ORG_ID = "org-test"
OTHER_ORG_ID = "org-other"

pytestmark = pytest.mark.anyio


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'catalog_repo.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


async def _seed(
    sf,
    *,
    entry_id: str,
    org_id: str = ORG_ID,
    workspace_id: str | None = None,
    resource_type: str = RESOURCE_TYPE_AGENT,
    resource_id: str | None = None,
    name: str = "n",
    source: str = SOURCE_DATABASE,
    status: str = CATALOG_STATUS_ACTIVE,
) -> CatalogEntryRow:
    async with sf() as session:
        row = CatalogEntryRow(
            id=entry_id,
            org_id=org_id,
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id or f"res-{entry_id}",
            name=name,
            display_name=name,
            source=source,
            status=status,
            metadata_={"k": "v"},
            synced_at=datetime.now(UTC),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


# ---------------------------------------------------------------------------
# list_catalog_entries (ART-1200)
# ---------------------------------------------------------------------------


class TestList:
    async def test_lists_only_caller_org(self, sf):
        await _seed(sf, entry_id="a", org_id=ORG_ID)
        await _seed(sf, entry_id="b", org_id=OTHER_ORG_ID)
        rows = await list_catalog_entries(sf, org_id=ORG_ID)
        assert {r.id for r in rows} == {"a"}

    async def test_defaults_to_active_only(self, sf):
        await _seed(sf, entry_id="act", status=CATALOG_STATUS_ACTIVE)
        await _seed(sf, entry_id="dis", status=CATALOG_STATUS_DISABLED)
        await _seed(sf, entry_id="arc", status=CATALOG_STATUS_ARCHIVED)
        rows = await list_catalog_entries(sf, org_id=ORG_ID)
        assert {r.id for r in rows} == {"act"}

    async def test_status_filter_overrides_default(self, sf):
        await _seed(sf, entry_id="act", status=CATALOG_STATUS_ACTIVE)
        await _seed(sf, entry_id="arc", status=CATALOG_STATUS_ARCHIVED)
        rows = await list_catalog_entries(sf, org_id=ORG_ID, status=CATALOG_STATUS_ARCHIVED)
        assert {r.id for r in rows} == {"arc"}

    async def test_status_empty_includes_all(self, sf):
        await _seed(sf, entry_id="act", status=CATALOG_STATUS_ACTIVE)
        await _seed(sf, entry_id="arc", status=CATALOG_STATUS_ARCHIVED)
        rows = await list_catalog_entries(sf, org_id=ORG_ID, status="")
        assert {r.id for r in rows} == {"act", "arc"}

    async def test_resource_type_filter(self, sf):
        await _seed(sf, entry_id="ag", resource_type=RESOURCE_TYPE_AGENT)
        await _seed(sf, entry_id="sk", resource_type=RESOURCE_TYPE_SKILL)
        rows = await list_catalog_entries(sf, org_id=ORG_ID, resource_type=RESOURCE_TYPE_AGENT)
        assert {r.id for r in rows} == {"ag"}

    async def test_workspace_filter(self, sf):
        await _seed(sf, entry_id="ws1", workspace_id="ws-1")
        await _seed(sf, entry_id="ws2", workspace_id="ws-2")
        await _seed(sf, entry_id="none", workspace_id=None)
        rows = await list_catalog_entries(sf, org_id=ORG_ID, workspace_id="ws-1")
        assert {r.id for r in rows} == {"ws1"}

    async def test_no_workspace_filter_returns_all(self, sf):
        await _seed(sf, entry_id="ws1", workspace_id="ws-1")
        await _seed(sf, entry_id="none", workspace_id=None)
        rows = await list_catalog_entries(sf, org_id=ORG_ID)
        assert {r.id for r in rows} == {"ws1", "none"}

    async def test_requires_org_id(self, sf):
        with pytest.raises(ValueError):
            await list_catalog_entries(sf, org_id="")


# ---------------------------------------------------------------------------
# get_catalog_entry (ART-1210)
# ---------------------------------------------------------------------------


class TestGet:
    async def test_returns_row_for_matching_org(self, sf):
        await _seed(sf, entry_id="a", resource_type=RESOURCE_TYPE_AGENT, resource_id="r-a")
        row = await get_catalog_entry(sf, org_id=ORG_ID, resource_type=RESOURCE_TYPE_AGENT, resource_id="r-a")
        assert row is not None
        assert row.id == "a"

    async def test_cross_org_returns_none(self, sf):
        await _seed(sf, entry_id="a", org_id=ORG_ID, resource_id="r-a")
        row = await get_catalog_entry(sf, org_id=OTHER_ORG_ID, resource_type=RESOURCE_TYPE_AGENT, resource_id="r-a")
        assert row is None

    async def test_missing_returns_none(self, sf):
        row = await get_catalog_entry(sf, org_id=ORG_ID, resource_type=RESOURCE_TYPE_AGENT, resource_id="ghost")
        assert row is None

    async def test_does_not_filter_status(self, sf):
        """get returns the row regardless of status (active-only is the
        caller's concern); a disabled entry is still gettable for admin ops."""
        await _seed(sf, entry_id="dis", status=CATALOG_STATUS_DISABLED, resource_id="r-dis")
        row = await get_catalog_entry(sf, org_id=ORG_ID, resource_type=RESOURCE_TYPE_AGENT, resource_id="r-dis")
        assert row is not None
        assert row.status == CATALOG_STATUS_DISABLED

    async def test_requires_org_id(self, sf):
        with pytest.raises(ValueError):
            await get_catalog_entry(sf, org_id="", resource_type=RESOURCE_TYPE_AGENT, resource_id="r")
