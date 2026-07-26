"""ORM models for the agent-artifact control-plane tables (PR-050 / PR-053).

Three layers of ADR-0004 live here:

* ``agent_packages`` / ``agent_versions`` (PR-050, data-model.md §6.2/§6.3) —
  the immutable-content backbone. ``AgentPackage`` is the stable logical
  identity; ``AgentVersion`` is the immutable content version whose
  ``digest`` is the execution identity.
* ``release_channels`` / ``release_events`` (PR-053, data-model.md §6.4/§6.5)
  — the mutable per-(org, workspace, package, channel) pointer
  (``current_version_id``) plus the append-only promote/rollback history.
  Channel updates go through Compare-And-Swap on ``row_version`` (the
  codebase's first CAS caller).

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
* ``release_channels`` carries a cross-dialect unique INDEX (not a
  table-level ``UniqueConstraint``) so NULL ``workspace_id`` collides on
  both Postgres (``NULLS NOT DISTINCT``) and SQLite (``COALESCE`` sentinel).
  See migration ``0013_release_channels_events`` for the dialect branch —
  the ORM deliberately omits the constraint so ``create_all`` does not emit
  a SQLite-incompatible plain UNIQUE.
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
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
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


# ---------------------------------------------------------------------------
# Channel layer (PR-053, ADR-0004 §5/§7/§8, data-model.md §6.4/§6.5)
# ---------------------------------------------------------------------------

#: Allowed channel values (ADR §5). Mirrored as module constants so callers
#: avoid string literals; the DB CHECK (``ck_release_channels_channel``) is
#: the authoritative closed set.
CHANNEL_DEV = "dev"
CHANNEL_STAGING = "staging"
CHANNEL_PROD = "prod"
_ALLOWED_CHANNELS: frozenset[str] = frozenset({CHANNEL_DEV, CHANNEL_STAGING, CHANNEL_PROD})

#: ReleaseEvent action constants (ADR §7/§8). ``promote`` advances the
#: pointer forward; ``rollback`` moves it to a historical version.
EVENT_ACTION_PROMOTE = "promote"
EVENT_ACTION_ROLLBACK = "rollback"


class ReleaseChannelRow(Base):
    """Mutable per-(org, workspace, package, channel) version pointer (data-model.md §6.4).

    Exactly one row per ``(org_id, workspace_id, package_id, channel)`` — the
    cross-dialect unique INDEX (``uq_release_channels_org_ws_pkg_channel``)
    enforces this even when ``workspace_id IS NULL`` (Postgres
    ``NULLS NOT DISTINCT``; SQLite ``COALESCE`` sentinel). The constraint is
    INDEX-based, not a table-level ``UniqueConstraint``, so ``create_all``
    does not emit a SQLite-incompatible plain UNIQUE — see migration
    ``0013_release_channels_events`` and the module docstring.

    ``current_version_id`` is the version this channel currently resolves;
    ``row_version`` is the optimistic-concurrency token for promote/rollback
    CAS (``WHERE row_version = :expected`` then check ``rowcount``). A NULL
    ``current_version_id`` means the channel exists but points at nothing
    yet (the initial state after :func:`get_or_create_channel`).
    """

    __tablename__ = "release_channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Optional workspace grouping — same soft-reference convention as
    # AgentPackageRow.workspace_id (no FK; isolation at the query layer).
    workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    package_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_packages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    # Polymorphic actor — last promoter/rollbacker. Mirrors AgentPackageRow.
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)

    __table_args__ = (
        CheckConstraint(
            "channel IN ('dev', 'staging', 'prod')",
            name="ck_release_channels_channel",
        ),
        # Cross-dialect unique INDEX for (org_id, workspace_id, package_id,
        # channel) with NULL-collision on workspace_id. The COALESCE
        # expression collapses NULL → '_default' so two NULL-workspace rows
        # collide on SQLite (which has no NULLS NOT DISTINCT clause). This
        # Index is emitted by create_all on both backends; migration 0013
        # ADDITIONALLY creates a native NULLS NOT DISTINCT index on Postgres
        # 15+ (the production-preferred form). A plain UniqueConstraint is
        # deliberately omitted — it would treat NULLs as distinct on SQLite
        # and break create_all↔migrated parity.
        Index(
            "uq_release_channels_org_ws_pkg_channel",
            "org_id",
            text("COALESCE(workspace_id, '_default')"),
            "package_id",
            "channel",
            unique=True,
        ),
        Index("idx_release_channels_lookup", "org_id", "package_id"),
        Index("idx_release_channels_org", "org_id"),
    )


class ReleaseEventRow(Base):
    """Append-only promote/rollback history (data-model.md §6.5, ADR-0004 §14).

    Distinct from the compliance-grade ``audit_events`` row: this is the
    domain history (who moved which channel from which version to which
    version, when, why). The router writes both in the same transaction —
    ``release.agent.published`` / ``release.agent.rolled_back`` on the audit
    side (ADR §14), this row on the domain side. ``from_version_id`` is
    NULL on the first promote (channel had no prior current version).
    """

    __tablename__ = "release_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    channel_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("release_channels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # NULL on first promote (channel had no prior current_version_id).
    from_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    to_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        CheckConstraint(
            "action IN ('promote', 'rollback')",
            name="ck_release_events_action",
        ),
        Index("idx_release_events_channel", "channel_id"),
        Index("idx_release_events_org", "org_id"),
    )


class ReleaseIdempotencyRecordRow(Base):
    """Idempotency-Key replay store for promote/rollback (ADR-0004 §7, PR-055).

    A replay record persists the **exact original response** (serialized
    ``PromoteResponse`` — channel + event) plus a ``request_hash`` of the
    semantically-meaningful request fields, so a retried promote/rollback with
    the same ``Idempotency-Key`` returns the original result without
    re-running ``_move_channel`` or emitting a second audit row. A same key
    with a different request surfaces ``idempotency_conflict``.

    The ``UNIQUE(org_id, idempotency_key)`` constraint is the concurrency
    fence: two concurrent same-key requests cannot both insert; the loser
    rolls back and either replays (identical request) or conflicts (different
    request). No FK — a replay record is self-contained (it carries its own
    response snapshot) and is independent of channel lifecycle. No TTL column;
    pruning old records is a follow-up.
    """

    __tablename__ = "release_idempotency_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # sha256 hex of the canonicalized request identity (see
    # ``_request_fingerprint`` in release/idempotency.py).
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # Serialized PromoteResponse. Replayed verbatim.
    response_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        UniqueConstraint("org_id", "idempotency_key", name="uq_release_idempotency_org_key"),
        Index("idx_release_idempotency_org", "org_id"),
        Index("idx_release_idempotency_org_key", "org_id", "idempotency_key"),
    )
