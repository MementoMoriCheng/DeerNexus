"""ORM models for the IAM control-plane tables (PR-020B).

These four tables — ``roles``, ``role_bindings``, ``service_accounts``,
``api_keys`` — form the RBAC / machine-identity backbone described in
``docs/architecture/data-model.md`` §5 and ADR-0003. They are introduced
additively (expand-only migration ``0004_iam_tables``); the tenant tables
from PR-020A (``organizations``, ``users``) are referenced as FK targets
without modification.

Cross-backend note: same conventions as ``orgs/model.py`` — ``JSON`` (not
``JSONB``), ``DateTime(timezone=True)``, ``String(36)`` UUIDs. Partial unique
indexes declare both ``sqlite_where`` and ``postgresql_where`` predicates.

Key design points (data-model.md §5, ADR-0003):

* ``roles.org_id`` is the **documented NULL exception** (§4.1 ADR-0002):
  system role templates (``org:admin`` / ``org:developer`` / ``org:viewer``)
  carry ``org_id IS NULL`` + ``is_system = true``; tenant roles have both
  set. A CHECK constraint enforces "``org_id IS NULL`` only when
  ``is_system``".
* ``role_bindings`` uses a **polymorphic principal** (``principal_type`` +
  ``principal_id``) with no FK to ``users`` / ``service_accounts`` —
  principal integrity is enforced by the write-service and DB triggers, not
  a client-only FK (§5.2). ``role_id`` IS an FK to ``roles``.
* ``api_keys`` stores only ``key_hash`` (strong KDF/HMAC); no recoverable
  full key. ``key_prefix`` is unique (lookup + display).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RoleRow(Base):
    """RBAC role definition — tenant role or system template (data-model.md §5.1)."""

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # org_id is nullable ONLY for system templates (is_system=true). Tenant
    # roles carry a non-null org_id. The CHECK constraint below enforces that
    # an org-scoped role cannot masquerade as system, and vice-versa.
    org_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    permissions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Tracks the seed revision that produced a builtin system template's
    # permission set (ADR-0003 §5: "内置角色变更必须有迁移、变更记录和回归
    # 测试"). NULL on custom roles — only builtin system templates carry it.
    # Introduced by PR-030 alongside the 0007_builtin_roles seed migration.
    template_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)

    __table_args__ = (
        # org_id IS NULL is allowed ONLY when is_system = true (ADR-0002 §4.1
        # system-global exception). A tenant role (org_id set, is_system false)
        # must not slip through with a NULL org, and a system template must not
        # carry an org_id.
        CheckConstraint(
            "(org_id IS NOT NULL) OR (is_system = 1)",
            name="ck_roles_system_template_allows_null_org",
        ),
        # Tenant roles are unique per (org_id, name). System templates have a
        # NULL org_id so they are excluded from this constraint via a partial
        # index (NULL org_id rows are unconstrained here).
        Index(
            "uq_roles_org_name",
            "org_id",
            "name",
            unique=True,
            sqlite_where=text("org_id IS NOT NULL"),
            postgresql_where=text("org_id IS NOT NULL"),
        ),
    )


class RoleBindingRow(Base):
    """Binds a principal (user / service_account) to a role within an org (§5.2)."""

    __tablename__ = "role_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Polymorphic: points to users.id or service_accounts.id depending on
    # principal_type. No FK constraint here — integrity is enforced by the
    # write-service and DB triggers (§5.2), not a client-only FK.
    principal_id: Mapped[str] = mapped_column(String(36), nullable=False)
    role_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "principal_type IN ('user', 'service_account')",
            name="ck_role_bindings_principal_type",
        ),
        UniqueConstraint(
            "org_id",
            "principal_type",
            "principal_id",
            "role_id",
            name="uq_role_bindings_org_principal_role",
        ),
        Index("idx_role_bindings_principal", "principal_type", "principal_id"),
        Index("idx_role_bindings_org", "org_id"),
    )


class ServiceAccountRow(Base):
    """Machine identity within an org (data-model.md §4.6, ADR-0003 §9)."""

    __tablename__ = "service_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # ADR-0003 §9.1 traceability fields (added by PR-034 via migration
    # ``0008_service_account_fields``). All nullable: an existing row
    # (none at PR-034 entry) and a future minimally-seeded row both
    # remain valid without these.
    #
    # ``owner_user_id`` is the accountability contact — "Owner 是管理
    # 责任人,不意味着自动拥有该账号权限" (ADR §9.1). Polymorphic
    # ``String(36)`` UUID with no FK, mirroring the principal convention
    # throughout the IAM tables. ``expires_at`` is a review-by date, not
    # a credential expiry — the ServiceAccount does not auto-expire.
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(256), nullable=True)
    system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_service_accounts_status",
        ),
        UniqueConstraint("org_id", "name", name="uq_service_accounts_org_name"),
        Index("idx_service_accounts_org", "org_id"),
    )


class ApiKeyRow(Base):
    """Hashed API key bound to a service account (data-model.md §4.7, ADR-0003 §9.2)."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    service_account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("service_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # Strong KDF or HMAC result — NEVER the recoverable full key.
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("key_prefix", name="uq_api_keys_key_prefix"),
        Index("idx_api_keys_org_sa", "org_id", "service_account_id"),
    )
