"""Request / response contracts for the AgentPackage / AgentVersion APIs (PR-052).

Pydantic envelopes for ``app/gateway/routers/agent_artifacts.py``. These
mirror the ``AgentPackageRow`` / ``AgentVersionRow`` columns landed by
PR-050 (migration ``0012_agent_artifacts``, ADR-0004 §3.1/§3.2) plus the
version-lifecycle state machine (ADR §4) and the content upload shape
(ADR §11.1 inline vs §11.2 object storage).

Kept in ``deerflow.contracts`` because the harness boundary
(``test_harness_boundary``) requires DTOs the app layer depends on to
live in contracts — the router imports these directly. The module imports
only Pydantic base types + ``datetime`` + stdlib, so it carries no ORM /
FastAPI / LangGraph dependency.

Design points (ADR-0004):

* ``version`` is a human-readable SemVer 2.0 display string — validated here
  by regex (no ``semver`` dependency). The immutable execution identity is
  ``digest`` (``sha256:<hex>``), computed by the repository from the artifact
  bytes, NOT supplied by the client.
* ``content`` is the raw artifact payload supplied on create. The repository
  routes it inline (small) or to the ObjectStore (large) per the production
  threshold; the response envelope never echoes raw content back — only
  ``digest`` + ``size_bytes`` + the storage pointer (``content_inline`` is
  server-internal, ``object_key`` is opaque to the caller).
* ``status`` transitions go through dedicated ``:review`` / ``:publish`` /
  ``:revoke`` endpoints (Google-AIP verbs), so the create/update envelopes
  deliberately omit ``status``.
* Once a Version enters ``published`` its content / manifest / digest /
  version are immutable (ADR §3.2 / §4.3) — enforced by the repository write
  path, not the DTO. The DTO only constrains the input shape.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# SemVer 2.0.0 — https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string
# core MAJOR.MINOR.PATCH + optional -prerelease + optional +build. Anchored.
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


class AgentPackageStatus(StrEnum):
    """Package lifecycle (ADR §3.1, data-model §6.2)."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class AgentVersionStatus(StrEnum):
    """Version content lifecycle (ADR §4).

    ``draft → reviewed → published → revoked``; ``draft | reviewed → archived``.
    The repository enforces legal transitions; the DTO only enumerates the
    closed set so a bad status string fails at the boundary.
    """

    DRAFT = "draft"
    REVIEWED = "reviewed"
    PUBLISHED = "published"
    REVOKED = "revoked"
    ARCHIVED = "archived"


class Manifest(BaseModel):
    """Agent artifact manifest (ADR-0004 §3.3).

    The MVP manifest is a structured declaration of the agent's entry point,
    dependencies, and runtime requirements. It MUST NOT carry Secret
    plaintext — secrets are referenced by stable id (``secret_requirements``
    carry a ``ref`` / ``name``, never a value). The manifest is part of the
    artifact content and participates in the digest.

    All fields beyond ``schema_version`` + ``agent_entry`` are optional so a
    minimal draft can be created and fleshed out before review. Lists default
    to empty (not None) so the digest is deterministic over a stable shape.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(min_length=1, description="Manifest schema version (e.g. 'v1alpha1').")
    agent_entry: str = Field(min_length=1, description="Entry point identifier within the artifact.")
    soul_or_prompt_ref: str | None = Field(default=None, description="Stable reference to the agent's soul/prompt; never plaintext.")
    model_requirements: list[dict] = Field(default_factory=list)
    skills: list[dict] = Field(default_factory=list, description="Skill references — stable id / version / digest.")
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[dict] = Field(default_factory=list, description="MCP server references — stable id / version.")
    dependencies: list[dict] = Field(default_factory=list, description="Explicit dependency locks.")
    network_requirements: list[dict] = Field(default_factory=list, description="Explicit network egress declarations.")
    secret_requirements: list[dict] = Field(default_factory=list, description="Secret references (name/ref only — no plaintext).")
    runtime_limits: dict | None = Field(default=None)
    source_metadata: dict | None = Field(default=None, description="Import provenance (path, upstream commit, import time) — not an execution identity.")


# ---------------------------------------------------------------------------
# AgentPackage envelopes
# ---------------------------------------------------------------------------


class AgentPackageCreateRequest(BaseModel):
    """Body of ``POST /api/v1/agent-packages``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120, description="Stable machine name; unique within the Org.")
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None)
    workspace_id: str | None = Field(default=None, description="Optional grouping; does not change Org ownership or RBAC scope.")


class AgentPackageUpdateRequest(BaseModel):
    """Body of ``PATCH /api/v1/agent-packages/{id}``.

    ``name`` / ``org_id`` / ``workspace_id`` are intentionally absent — the
    stable identity is immutable once created (ADR §3.1: "name in Org is the
    stable machine identifier"). ``status`` is absent too; lifecycle goes
    through ``:archive``.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class AgentPackageResponse(BaseModel):
    """Response envelope for AgentPackage reads.

    1:1 projection of ``AgentPackageRow`` so the API and ORM cannot drift
    silently. ``from_attributes=True`` lets the router build it directly off
    the row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    workspace_id: str | None
    name: str
    display_name: str
    description: str | None
    status: str
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    row_version: int


# ---------------------------------------------------------------------------
# AgentVersion envelopes
# ---------------------------------------------------------------------------


class AgentVersionCreateRequest(BaseModel):
    """Body of ``POST /api/v1/agent-packages/{pkg_id}/versions``.

    The client supplies ``version`` (SemVer display) + ``manifest`` +
    ``content`` (raw artifact bytes). The repository computes ``digest``
    (``sha256:<hex>`` over the content) and routes storage inline or to the
    ObjectStore per the production threshold — neither digest nor the storage
    pointer is client-supplied.

    ``content`` is the artifact payload as a UTF-8 string (the inline Text
    column stores UTF-8; binary artifacts should be base64-encoded by the
    caller until a binary-safe upload path lands).
    """

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64, description="SemVer 2.0 display string.")
    manifest: Manifest
    content: str = Field(min_length=1, description="Raw artifact payload (UTF-8). Digest is computed over its UTF-8 bytes.")

    @field_validator("version")
    @classmethod
    def _version_must_be_semver(cls, value: str) -> str:
        if not _SEMVER_RE.match(value):
            raise ValueError("version must be a valid SemVer 2.0.0 string (MAJOR.MINOR.PATCH[-prerelease][+build])")
        return value


class AgentVersionResponse(BaseModel):
    """Response envelope for AgentVersion reads.

    1:1 projection of ``AgentVersionRow`` EXCEPT ``content_inline`` is
    deliberately omitted — the raw artifact payload is server-internal; the
    caller observes the immutable identity (``digest``), size, and status.
    ``object_key`` is included so a future signed-URL download path can
    reference it, but it is opaque to the caller.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    package_id: str
    version: str
    digest: str
    status: str
    manifest: dict
    object_key: str | None
    size_bytes: int
    created_by: str | None
    created_at: datetime
    published_at: datetime | None
    revoked_at: datetime | None
