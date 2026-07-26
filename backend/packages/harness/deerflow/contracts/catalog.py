"""Catalog discovery-index contracts (PR-054).

Pydantic envelope for ``GET /api/v1/orgs/{org_id}/catalog`` (data-model.md
§6.6, ADR-0004 §10). The Catalog is a cross-resource discovery index; this
module owns the read-side response shape only (the write path that projects
into ``catalog_entries`` lands in a follow-up).

Kept in ``deerflow.contracts`` because the harness boundary
(``test_harness_boundary``) requires DTOs the app layer depends on to live in
contracts — the catalog router imports this directly. The module imports only
Pydantic base types + ``datetime``, so it carries no ORM / FastAPI dependency.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class CatalogEntryResponse(BaseModel):
    """Response envelope for a ``catalog_entries`` row (data-model.md §6.6).

    1:1 projection of ``CatalogEntryRow`` so the API and ORM cannot drift
    silently. ``from_attributes=True`` lets the router build it directly off
    the row. ``metadata`` is the JSON discovery blob (non-sensitive — display
    hints, tags; secrets never land here).

    The ``metadata`` field uses ``AliasChoices`` because the ORM exposes the
    column under the Python attribute ``catalog_entry_metadata`` (the DB
    column is ``metadata`` — data-model.md §6.6 — but ``Base`` already owns
    ``metadata`` as the SQLAlchemy ``MetaData``, so the ORM attribute is
    renamed to ``metadata_`` and re-exposed via the ``catalog_entry_metadata``
    property). The API-facing field stays ``metadata`` to match the documented
    contract; the alias only affects ORM-row → envelope resolution.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: str
    org_id: str
    workspace_id: str | None
    resource_type: str
    resource_id: str
    name: str
    display_name: str
    source: str
    status: str
    metadata: dict = Field(
        validation_alias=AliasChoices("catalog_entry_metadata", "metadata_"),
        description="Non-sensitive discovery metadata (display hints, tags).",
        serialization_alias="metadata",
    )
    synced_at: datetime
