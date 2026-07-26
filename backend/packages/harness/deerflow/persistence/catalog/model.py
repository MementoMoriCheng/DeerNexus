"""ORM model for the ``catalog_entries`` discovery-index table (PR-054).

data-model.md §6.6. The Catalog is a cross-resource discovery index (agents +
skills + mcp + tools) — NOT execution authority. Execution resolves a
``ReleaseRef`` via :class:`app.gateway.release_resolver.DbReleaseResolver`;
the Catalog only lets operators browse what exists in their Org.

Conventions mirror ``release/model.py`` / ``iam/model.py``: ``JSON`` (not
``JSONB``), ``DateTime(timezone=True)``, ``String(36)`` UUIDs. The table is
portable across the SQLite test backend and Postgres production backend.

Key design points (data-model.md §6.6, ADR-0004 §10):

* ``resource_id`` is a **soft reference** (no FK) — ADR §10 "Catalog 是发现
  索引,不是执行权威". The writer (import / promote projection, follow-up)
  owns integrity.
* The closed sets (``resource_type`` / ``source`` / ``status``) are enforced
  by DB CHECKs so a malformed row fails at insert.
* The ``(org_id, resource_type, resource_id)`` UNIQUE is plain (no nullable
  column participates), unlike ``release_channels`` (PR-053).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base

#: Closed set for ``resource_type`` (data-model.md §6.6).
RESOURCE_TYPE_AGENT = "agent"
RESOURCE_TYPE_SKILL = "skill"
RESOURCE_TYPE_MCP = "mcp"
RESOURCE_TYPE_TOOL = "tool"
_ALLOWED_RESOURCE_TYPES: frozenset[str] = frozenset({RESOURCE_TYPE_AGENT, RESOURCE_TYPE_SKILL, RESOURCE_TYPE_MCP, RESOURCE_TYPE_TOOL})

#: Closed set for ``source`` — where the catalog row was projected from.
SOURCE_DATABASE = "database"  # created via the studio API (PR-052 create_package)
SOURCE_FILE_IMPORT = "file_import"  # projected from import_agent_from_file (PR-051)
SOURCE_SYSTEM = "system"  # seeded by the platform (e.g. builtin skills)
_ALLOWED_SOURCES: frozenset[str] = frozenset({SOURCE_DATABASE, SOURCE_FILE_IMPORT, SOURCE_SYSTEM})

#: Closed set for ``status`` — the row's discovery lifecycle (NOT the
#: underlying resource's lifecycle; a disabled catalog row hides the resource
#: from ``GET /catalog`` without deleting it).
CATALOG_STATUS_ACTIVE = "active"
CATALOG_STATUS_DISABLED = "disabled"
CATALOG_STATUS_ARCHIVED = "archived"
_ALLOWED_CATALOG_STATUSES: frozenset[str] = frozenset({CATALOG_STATUS_ACTIVE, CATALOG_STATUS_DISABLED, CATALOG_STATUS_ARCHIVED})


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CatalogEntryRow(Base):
    """One row in the cross-resource discovery index (data-model.md §6.6).

    ``resource_id`` is a soft reference to the underlying resource (e.g. an
    ``agent_packages.id`` for ``resource_type='agent'``); there is NO FK
    because the Catalog is not execution authority (ADR §10). ``metadata`` is
    non-sensitive discovery info (display hints, tags); secrets never land
    here. ``synced_at`` records when the writer last projected the row, so a
    stale Catalog entry is detectable.
    """

    __tablename__ = "catalog_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Optional workspace grouping — same soft-reference convention as
    # AgentPackageRow.workspace_id (no FK; isolation at the query layer).
    workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=CATALOG_STATUS_ACTIVE)
    # The DB column is ``metadata`` (data-model.md §6.6), but the Python
    # attribute is ``metadata_`` — ``Base`` (DeclarativeBase) already owns a
    # class-level ``metadata`` (the SQLAlchemy ``MetaData``), and shadowing it
    # would break table registration. The ``catalog_entry_metadata`` property
    # below re-exposes the dict under the API-facing name so
    # ``CatalogEntryResponse.model_validate(row)`` (``from_attributes=True``)
    # resolves ``metadata`` to the dict, not to ``Base.metadata``.
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)

    @property
    def catalog_entry_metadata(self) -> dict:
        """The discovery metadata dict (column ``metadata``).

        Named distinctly from ``metadata`` to avoid shadowing ``Base.metadata``
        (the SQLAlchemy ``MetaData``); the catalog router/contract reads this.
        """
        return self.metadata_

    __table_args__ = (
        CheckConstraint(
            "resource_type IN ('agent', 'skill', 'mcp', 'tool')",
            name="ck_catalog_entries_resource_type",
        ),
        CheckConstraint(
            "source IN ('database', 'file_import', 'system')",
            name="ck_catalog_entries_source",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled', 'archived')",
            name="ck_catalog_entries_status",
        ),
        UniqueConstraint(
            "org_id",
            "resource_type",
            "resource_id",
            name="uq_catalog_entries_org_resource",
        ),
        Index("idx_catalog_entries_org", "org_id"),
        Index("idx_catalog_entries_org_type", "org_id", "resource_type"),
        Index("idx_catalog_entries_workspace", "org_id", "workspace_id"),
    )
