"""Authentication configuration for DeerFlow."""

import logging
import os
import secrets

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_SECRET_FILE = ".jwt_secret"
_API_KEY_PEPPER_FILE = ".api_key_pepper"


class AuthConfig(BaseModel):
    """JWT and auth-related configuration. Parsed once at startup.

    Note: the ``users`` table now lives in the shared persistence
    database managed by ``deerflow.persistence.engine``. The old
    ``users_db_path`` config key has been removed — user storage is
    configured through ``config.database`` like every other table.
    """

    jwt_secret: str = Field(
        ...,
        description="Secret key for JWT signing. MUST be set via AUTH_JWT_SECRET.",
    )
    token_expiry_days: int = Field(default=7, ge=1, le=30)
    oauth_github_client_id: str | None = Field(default=None)
    oauth_github_client_secret: str | None = Field(default=None)
    api_key_pepper: str = Field(
        default="",
        description=(
            "Server-side pepper for API key HMAC (PR-035). SHOULD be set via "
            "AUTH_API_KEY_PEPPER. Empty string is the test/default sentinel: "
            "``get_auth_config``'s loader auto-generates + persists a stable "
            "value when the env var is unset, so direct "
            "``AuthConfig(jwt_secret=...)`` test constructions keep working. "
            "NOT reused as jwt_secret (defense-in-depth)."
        ),
    )


_auth_config: AuthConfig | None = None


def _load_or_create_secret(secret_filename: str, *, env_hint: str) -> str:
    """Load a persisted secret from ``{base_dir}/<filename>``, or generate and persist a new one.

    Shared body of the JWT secret + API key pepper loaders. Each secret
    is persisted in its own file under 0o600 so a deploy that loses its
    env vars still converges on a stable secret across restarts. The
    ``env_hint`` is used in the warning log so an operator knows which
    env var to set in production.
    """
    from deerflow.config.paths import get_paths

    paths = get_paths()
    secret_file = paths.base_dir / secret_filename

    try:
        if secret_file.exists():
            secret = secret_file.read_text(encoding="utf-8").strip()
            if secret:
                return secret
    except OSError as exc:
        raise RuntimeError(f"Failed to read secret from {secret_file}. Set {env_hint} explicitly or fix DEER_FLOW_HOME/base directory permissions so DeerFlow can read its persisted auth secret.") from exc

    secret = secrets.token_urlsafe(32)
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(secret)
    except OSError as exc:
        raise RuntimeError(f"Failed to persist secret to {secret_file}. Set {env_hint} explicitly or fix DEER_FLOW_HOME/base directory permissions so DeerFlow can store a stable auth secret.") from exc
    return secret


def get_auth_config() -> AuthConfig:
    """Get the global AuthConfig instance. Parses from env on first call."""
    global _auth_config
    if _auth_config is None:
        from dotenv import load_dotenv

        load_dotenv()
        jwt_secret = os.environ.get("AUTH_JWT_SECRET")
        if not jwt_secret:
            jwt_secret = _load_or_create_secret(_SECRET_FILE, env_hint="AUTH_JWT_SECRET")
            os.environ["AUTH_JWT_SECRET"] = jwt_secret
            logger.warning(
                "⚠ AUTH_JWT_SECRET is not set — using an auto-generated secret "
                "persisted to .jwt_secret. Sessions will survive restarts. "
                "For production, add AUTH_JWT_SECRET to your .env file: "
                'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        api_key_pepper = os.environ.get("AUTH_API_KEY_PEPPER")
        if not api_key_pepper:
            api_key_pepper = _load_or_create_secret(_API_KEY_PEPPER_FILE, env_hint="AUTH_API_KEY_PEPPER")
            os.environ["AUTH_API_KEY_PEPPER"] = api_key_pepper
            logger.warning(
                "⚠ AUTH_API_KEY_PEPPER is not set — using an auto-generated pepper "
                "persisted to .api_key_pepper. Existing API keys will verify across "
                "restarts. For production, add AUTH_API_KEY_PEPPER to your .env file: "
                'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        _auth_config = AuthConfig(jwt_secret=jwt_secret, api_key_pepper=api_key_pepper)
    return _auth_config


def set_auth_config(config: AuthConfig) -> None:
    """Set the global AuthConfig instance (for testing)."""
    global _auth_config
    _auth_config = config
