"""ORM model for user-owned custom model-provider rows."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ModelProviderRow(Base):
    """A private, user-owned custom model supplier (OpenAI-compatible).

    Each row is scoped to ``owner_user_id`` (the per-user isolation boundary).
    The per-request config-merge middleware reads a user's rows and injects
    them into the request-scoped ``AppConfig.models`` list so the model is
    selectable in chat and the LLM call chain can actually invoke it.

    ``encrypted_api_key`` stores a Fernet ciphertext (``fernet:v1:...``);
    the cleartext key never reaches the DB — encrypt/decrypt happens in the
    repository via the shared ``ChannelCredentialCipher``.

    Cross-backend conventions match the rest of the control plane:
    ``String(36)`` UUIDs, ``DateTime(timezone=True)``, ``Text`` for the
    ciphertext so the table is portable across SQLite (test) and Postgres
    (production).
    """

    __tablename__ = "model_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    use: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    supports_thinking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    supports_reasoning_effort: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_model_providers_owner_name"),
    )
