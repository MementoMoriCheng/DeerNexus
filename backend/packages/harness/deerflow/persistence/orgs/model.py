"""ORM models for the tenant control-plane tables (PR-020A).

These four tables — ``organizations``, ``workspaces``,
``external_identities``, ``org_memberships`` — form the tenant identity
backbone described in ``docs/architecture/data-model.md`` §4. They are
introduced additively (expand-only migration ``0003_tenant_tables``); no
existing DeerFlow resource table is touched here, and the ``users`` table
(created by ``0001_baseline``) is referenced as an FK target without
modification.

Cross-backend note: ``data-model.md`` targets PostgreSQL (``jsonb`` /
``citext`` / ``timestamptz``). The models here use cross-dialect types
(``JSON``, ``DateTime(timezone=True)``, ``String(n)``) so the test suite
(aiosqlite) and Postgres deployments stay in parity — ``env.py``'s
``_type_equivalent`` treats ``JSON`` and ``JSONB`` as equivalent. Partial
unique indexes declare both ``sqlite_where`` and ``postgresql_where``
predicates (same pattern as ``channel_connections``).

UUIDs are stored as ``String(36)`` for cross-backend portability, matching
the existing ``UserRow`` convention.
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
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OrganizationRow(Base):
    """Top-level tenant boundary (data-model.md §4.1)."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended', 'deleting', 'deleted')",
            name="ck_organizations_status",
        ),
        # Platform-unique slug, but only among non-deleted orgs: a soft-deleted
        # org keeps its row (for audit / UUID non-reuse) but releases its slug
        # so a new org may claim it. Partial unique indexes are supported by
        # both SQLite (>= 3.8.0) and PostgreSQL.
        Index(
            "uq_organizations_slug_active",
            "slug",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class WorkspaceRow(Base):
    """Optional grouping within an org (data-model.md §4.2)."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_workspaces_status",
        ),
        UniqueConstraint("org_id", "slug", name="uq_workspaces_org_slug"),
        Index("idx_workspaces_org_status", "org_id", "status"),
    )


class ExternalIdentityRow(Base):
    """Federated (OIDC) identity linked to a platform user (§4.4)."""

    __tablename__ = "external_identities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    claims_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)

    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_external_identities_issuer_subject"),
        Index("idx_external_identities_user", "user_id"),
    )


class OrgMembershipRow(Base):
    """A user's membership in an org, with lifecycle status (§4.5)."""

    __tablename__ = "org_memberships"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="invited")

    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'removed')",
            name="ck_org_memberships_status",
        ),
        UniqueConstraint("org_id", "user_id", name="uq_org_memberships_org_user"),
        Index("idx_org_memberships_user_status", "user_id", "status"),
    )
