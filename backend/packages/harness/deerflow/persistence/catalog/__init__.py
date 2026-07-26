"""Catalog discovery index: ORM model + read-side repository (PR-054).

Re-exports:

* the ORM row class (PR-054) so ``deerflow.persistence.models`` registers it
  with ``Base.metadata`` in a single import;
* the closed-set constants (``resource_type`` / ``source`` / ``status``);
* the read-side repository (``list_catalog_entries`` / ``get_catalog_entry``).

The write path (import / promote projecting into the Catalog) lands in a
follow-up; this PR ships the table + the ``GET /catalog`` reader, so the
endpoint returns ``[]`` until the writer is wired.
"""

from deerflow.persistence.catalog.model import (
    CATALOG_STATUS_ACTIVE,
    CATALOG_STATUS_ARCHIVED,
    CATALOG_STATUS_DISABLED,
    RESOURCE_TYPE_AGENT,
    RESOURCE_TYPE_MCP,
    RESOURCE_TYPE_SKILL,
    RESOURCE_TYPE_TOOL,
    SOURCE_DATABASE,
    SOURCE_FILE_IMPORT,
    SOURCE_SYSTEM,
    CatalogEntryRow,
)
from deerflow.persistence.catalog.repository import (
    get_catalog_entry,
    list_catalog_entries,
)

__all__ = [
    # ORM model (PR-054)
    "CatalogEntryRow",
    # closed-set constants
    "CATALOG_STATUS_ACTIVE",
    "CATALOG_STATUS_DISABLED",
    "CATALOG_STATUS_ARCHIVED",
    "RESOURCE_TYPE_AGENT",
    "RESOURCE_TYPE_MCP",
    "RESOURCE_TYPE_SKILL",
    "RESOURCE_TYPE_TOOL",
    "SOURCE_DATABASE",
    "SOURCE_FILE_IMPORT",
    "SOURCE_SYSTEM",
    # repository (PR-054 read-side)
    "get_catalog_entry",
    "list_catalog_entries",
]
