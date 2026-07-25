"""ORM models for the agent-artifact control-plane tables (PR-050).

These two tables — ``agent_packages`` and ``agent_versions`` — are the
immutable-content backbone described in ``docs/architecture/data-model.md``
§6.2/§6.3 and ADR-0004 §3. They are introduced additively (expand-only
migration ``0012_agent_artifacts``); no existing table is modified.

Cross-backend note: same conventions as ``iam/model.py`` / ``orgs/model.py``
— ``JSON`` (not ``JSONB``), ``DateTime(timezone=True)``, ``String(36)``
UUIDs. Large-content payloads use ``Text`` (not ``LargeBinary``/``bytea``)
so the table is portable across the SQLite test backend and Postgres
production backend; object storage keys live in ``object_key`` for large
artifacts (ADR-0004 §11).

Key design points (data-model.md §6, ADR-0004 §3):

* ``AgentPackage`` is the stable logical identity (``name`` unique per Org);
  ``AgentVersion`` is the immutable content version.
* ``digest`` (``sha256:<hex>``) is the immutable **execution identity**;
  ``version`` is a human-readable SemVer display string only (no DB CHECK —
  SemVer enforcement is a write-path concern, deferred to PR-052, mirroring
  ``contracts/release.py`` which treats ``version`` as display-only).
* ``content_inline`` and ``object_key`` are mutually exclusive and at least
  one is required — enforced by a cross-dialect CHECK (``ck_..._content_exclusive``)
  using the explicit ``IS [NOT] NULL`` form (safe on both SQLite and Postgres).
* Once an ``AgentVersion`` enters ``published``, its content / manifest /
  digest / version are immutable (ADR §3.2 / §4.3). That write-side freeze is
  **not** enforced by this schema PR — it is a repository-level concern and
  lands in PR-052. The DB CHECK here only constrains ``status`` to the
  state machine (``draft → reviewed → published → revoked``, plus
  ``draft | reviewed → archived``, ADR §4).

This PR lands the tables only — there are **no callers** yet (no repository,
router, or contract envelope). It follows the "land the table first"
philosophy established by PR-030 (builtin-roles table) and PR-040
(audit_events table).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AgentPackageRow(Base):
    """Stable logical identity of an agent within an Org (data-model.md §6.2).

    ``name`` is the stable machine identifier (unique per Org);
    ``display_name`` is the human label. ``workspace_id`` is an optional
    grouping that does not change Org-level ownership or permissions
    (ADR-0002 §4.1). A Package with existing Versions cannot be hard-deleted
    — enforced by the ``agent_versions.package_id`` FK ``ON DELETE RESTRICT``
    (ADR §3.1).
    """

    __tablename__ = "agent_packages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Optional workspace grouping — does not change Org ownership or RBAC
    # scope (ADR-0002 §4.1). Soft reference (no FK) so a Package can outlive
    # a Workspace row's lifecycle; isolation is enforced at the query layer.
    workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # Polymorphic actor — mirrors the IAM principal convention (no FK);
    # integrity enforced by the write service (PR-052).
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)
    # Optimistic-concurrency version. PR-050 has no CAS caller (promote /
    # rollback CAS lands in PR-053 on release_channels), but data-model §6.2
    # defines the column, so it is part of the frozen schema.
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_agent_packages_status",
        ),
        UniqueConstraint("org_id", "name", name="uq_agent_packages_org_name"),
        Index("idx_agent_packages_org", "org_id"),
    )


class AgentVersionRow(Base):
    """Immutable content version of an AgentPackage (data-model.md §6.3, ADR-0004 §3.2).

    ``digest`` is the immutable execution identity (``sha256:<hex>``);
    ``version`` is a SemVer display string only. Exactly one of
    ``content_inline`` (small artifact, stored inline) or ``object_key``
    (large artifact, object-storage reference) must be set — enforced by
    ``ck_agent_versions_content_exclusive``.

    The ``published``-immutability freeze (content / manifest / digest /
    version cannot change once ``status='published'``) is a write-path
    concern enforced by the repository in PR-052; this schema PR only
    constrains ``status`` to the ADR §4 state machine. There are no write
    callers in this PR.
    """

    __tablename__ = "agent_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Redundant org_id for forced isolation (same pattern as
    # threads_meta / runs — data-model §6.3 "冗余用于强制隔离"). Denormalised
    # from the parent package so cross-Org queries are filtered without a
    # join, and a Package's org cannot be spoofed by a Version row.
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    package_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_packages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    digest: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Exactly one of content_inline / object_key is required — see CHECK
    # below. content_inline is Text (not LargeBinary) so the table is
    # portable across SQLite and Postgres; artifact bytes are UTF-8 encoded.
    content_inline: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'reviewed', 'published', 'revoked', 'archived')",
            name="ck_agent_versions_status",
        ),
        # content_inline XOR object_key — exactly one must be set. Uses the
        # explicit IS [NOT] NULL form so it evaluates identically on SQLite
        # and Postgres (avoiding the three-valued-logic pitfall of bare
        # ``col1 <> col2`` with NULLs).
        CheckConstraint(
            "(content_inline IS NOT NULL AND object_key IS NULL) OR (content_inline IS NULL AND object_key IS NOT NULL)",
            name="ck_agent_versions_content_exclusive",
        ),
        CheckConstraint(
            "size_bytes >= 0",
            name="ck_agent_versions_size_nonneg",
        ),
        UniqueConstraint(
            "org_id",
            "package_id",
            "version",
            name="uq_agent_versions_pkg_version",
        ),
        UniqueConstraint("org_id", "digest", name="uq_agent_versions_org_digest"),
        Index("idx_agent_versions_org", "org_id"),
        Index("idx_agent_versions_package", "package_id"),
        Index("idx_agent_versions_org_status", "org_id", "status"),
    )
