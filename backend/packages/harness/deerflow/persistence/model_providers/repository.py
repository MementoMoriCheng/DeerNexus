"""SQL repository for user-owned custom model providers.

Encrypts the API key with the shared ``ChannelCredentialCipher`` so the
cleartext never reaches the database. Callers see the decrypted key only via
the explicit ``ModelProviderRecord.api_key`` field (used by the config-merge
middleware to construct a ``ModelConfig``); the CRUD router never returns it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.channel_connections.sql import ChannelCredentialCipher
from deerflow.persistence.model_providers.model import ModelProviderRow

logger = logging.getLogger(__name__)


@dataclass
class ModelProviderRecord:
    """Decrypted view of a provider row — ``api_key`` is the cleartext secret.

    Returned only to the config-merge middleware; the CRUD router serialises
    via ``ModelProviderResponse`` which omits the key (exposing ``has_api_key``
    instead).
    """

    id: str
    owner_user_id: str
    name: str
    display_name: str | None
    description: str | None
    model: str
    use: str
    base_url: str | None
    api_key: str
    supports_thinking: bool
    supports_reasoning_effort: bool


def _row_to_record(row: ModelProviderRow, cipher: ChannelCredentialCipher) -> ModelProviderRecord:
    return ModelProviderRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        model=row.model,
        use=row.use,
        base_url=row.base_url,
        api_key=cipher.decrypt_text(row.encrypted_api_key) or "",
        supports_thinking=row.supports_thinking,
        supports_reasoning_effort=row.supports_reasoning_effort,
    )


class ModelProviderRepository:
    """CRUD facade for per-user custom model providers."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cipher: ChannelCredentialCipher,
    ) -> None:
        self.session_factory = session_factory
        self._cipher = cipher

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex

    async def list_by_user(self, owner_user_id: str) -> list[ModelProviderRecord]:
        """Return all providers owned by ``owner_user_id`` (decrypted)."""
        async with self.session_factory() as session:
            rows = (await session.scalars(select(ModelProviderRow).where(ModelProviderRow.owner_user_id == owner_user_id).order_by(ModelProviderRow.created_at))).all()
            return [_row_to_record(row, self._cipher) for row in rows]

    async def get(self, owner_user_id: str, name: str) -> ModelProviderRecord | None:
        async with self.session_factory() as session:
            row = (
                await session.scalars(
                    select(ModelProviderRow).where(
                        ModelProviderRow.owner_user_id == owner_user_id,
                        ModelProviderRow.name == name,
                    )
                )
            ).first()
            return _row_to_record(row, self._cipher) if row else None

    async def get_by_id(self, owner_user_id: str, provider_id: str) -> ModelProviderRecord | None:
        async with self.session_factory() as session:
            row = (
                await session.scalars(
                    select(ModelProviderRow).where(
                        ModelProviderRow.owner_user_id == owner_user_id,
                        ModelProviderRow.id == provider_id,
                    )
                )
            ).first()
            return _row_to_record(row, self._cipher) if row else None

    async def create(
        self,
        *,
        owner_user_id: str,
        name: str,
        model: str,
        api_key: str,
        display_name: str | None = None,
        description: str | None = None,
        use: str = "langchain_openai:ChatOpenAI",
        base_url: str | None = None,
        supports_thinking: bool = False,
        supports_reasoning_effort: bool = False,
    ) -> ModelProviderRecord:
        """Insert a new provider row. Raises ``IntegrityError`` on name clash."""
        row = ModelProviderRow(
            id=self._new_id(),
            owner_user_id=owner_user_id,
            name=name,
            display_name=display_name,
            description=description,
            model=model,
            use=use,
            base_url=base_url,
            encrypted_api_key=self._cipher.encrypt_text(api_key) or "",
            supports_thinking=supports_thinking,
            supports_reasoning_effort=supports_reasoning_effort,
        )
        async with self.session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
            return _row_to_record(row, self._cipher)

    async def update(
        self,
        *,
        owner_user_id: str,
        provider_id: str,
        api_key: str | None = None,
        **fields: object,
    ) -> ModelProviderRecord | None:
        """Update editable fields. ``api_key=None`` leaves the key unchanged.

        ``api_key`` is re-encrypted when provided. Returns ``None`` if the row
        is not found or not owned by ``owner_user_id``.
        """
        async with self.session_factory() as session:
            row = (
                await session.scalars(
                    select(ModelProviderRow).where(
                        ModelProviderRow.owner_user_id == owner_user_id,
                        ModelProviderRow.id == provider_id,
                    )
                )
            ).first()
            if row is None:
                return None
            for key, value in fields.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            if api_key is not None:
                row.encrypted_api_key = self._cipher.encrypt_text(api_key) or ""
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
            return _row_to_record(row, self._cipher)

    async def delete(self, owner_user_id: str, provider_id: str) -> bool:
        """Delete a provider owned by ``owner_user_id``. Returns whether a row was removed."""
        async with self.session_factory() as session:
            result = await session.execute(
                delete(ModelProviderRow).where(
                    ModelProviderRow.owner_user_id == owner_user_id,
                    ModelProviderRow.id == provider_id,
                )
            )
            await session.commit()
            return (result.rowcount or 0) > 0
