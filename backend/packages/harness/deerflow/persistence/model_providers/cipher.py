"""Cipher factory for model-provider API-key encryption.

Resolves the Fernet master key from the ``DEERNEXUS_MODEL_PROVIDER_KEY``
environment variable and reuses the shared ``ChannelCredentialCipher``
(``from_key`` → sha256-derived Fernet key, ``fernet:v1:`` envelope). When the
key is absent the module falls back to a derived dev-only key and logs a
warning — mirroring the ``env_dev_only`` secret-store philosophy so local
development works without configuration while production is expected to set
the variable.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.channel_connections.sql import ChannelCredentialCipher

if TYPE_CHECKING:
    from deerflow.persistence.model_providers.repository import ModelProviderRepository

logger = logging.getLogger(__name__)

_ENV_KEY = "DEERNEXUS_MODEL_PROVIDER_KEY"

_DEV_FALLBACK_KEY = "deernexus-dev-model-provider-key"


def get_model_provider_cipher() -> ChannelCredentialCipher:
    """Return the cipher for encrypting/decrypting model-provider API keys.

    Reads ``DEERNEXUS_MODEL_PROVIDER_KEY``; falls back to a deterministic
    dev-only key (logged once) when unset so local development works without
    configuration. Production should always set the env var.
    """
    key = os.environ.get(_ENV_KEY)
    if key:
        return ChannelCredentialCipher.from_key(key)
    # Dev fallback — deterministic so a restart can still decrypt existing rows
    # on a single-node dev setup. Logged at warning level so operators notice.
    if not getattr(get_model_provider_cipher, "_warned_dev_key", False):
        logger.warning(
            "%s is not set — using a derived dev-only key for model-provider API-key encryption. Set this env var in production.",
            _ENV_KEY,
        )
        get_model_provider_cipher._warned_dev_key = True  # type: ignore[attr-defined]
    return ChannelCredentialCipher.from_key(_DEV_FALLBACK_KEY)


def build_repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> ModelProviderRepository:
    """Construct a ``ModelProviderRepository`` wired with the env-derived cipher."""
    from deerflow.persistence.model_providers.repository import ModelProviderRepository

    return ModelProviderRepository(session_factory, cipher=get_model_provider_cipher())


__all__ = [
    "build_repository",
    "get_model_provider_cipher",
]
