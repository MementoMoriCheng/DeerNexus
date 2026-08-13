"""Tests for the OIDC Bearer JWT credential path in the auth middleware.

Focuses on routing logic (API key vs JWT disambiguation) and the
``_resolve_oidc_bearer`` return-value contract (None when OIDC is not
configured, JSONResponse on failure). The core JWKS verification is tested
in ``test_oidc_jwks.py``; the doctor probe is tested in ``test_doctor_probes.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

from app.gateway.auth_middleware import _API_KEY_BEARER_PREFIX, _strip_bearer


class TestStripBearer:
    """The ``_strip_bearer`` helper is reused by all bearer paths."""

    def test_returns_token_for_bearer_scheme(self):
        assert _strip_bearer("Bearer abc.def.ghi") == "abc.def.ghi"

    def test_returns_empty_for_missing_header(self):
        assert _strip_bearer(None) == ""

    def test_returns_empty_for_non_bearer_scheme(self):
        assert _strip_bearer("Basic abc") == ""

    def test_is_case_insensitive_for_scheme(self):
        assert _strip_bearer("bearer token123") == "token123"

    def test_strips_whitespace(self):
        assert _strip_bearer("Bearer  spaced  ") == "spaced"


class TestApiKeyBearerRouting:
    """Bearer tokens are routed based on the ``dk_live_`` prefix."""

    def test_api_key_prefix_constant(self):
        """The prefix must match what ``generate_api_key`` produces."""
        assert _API_KEY_BEARER_PREFIX == "dk_live_"

    def test_dk_live_token_is_api_key_not_jwt(self):
        """A ``dk_live_`` token is an API key, not a JWT candidate."""
        token = _strip_bearer("Bearer dk_live_abcd1234_secret")
        assert token.startswith(_API_KEY_BEARER_PREFIX)

    def test_jwt_token_is_not_api_key(self):
        """A dot-separated JWT does not start with ``dk_live_``."""
        token = _strip_bearer("Bearer eyJhbGci.payload.signature")
        assert not token.startswith(_API_KEY_BEARER_PREFIX)


class TestResolveOidcBearer:
    """``_resolve_oidc_bearer`` routing when OIDC is not configured."""

    @pytest.mark.anyio
    async def test_returns_none_when_oidc_not_configured(self, monkeypatch):
        """When ``production.oidc`` is None the function returns None so
        the bearer token falls through to session/internal paths."""
        from app.gateway.auth_middleware import _resolve_oidc_bearer

        # Mock get_app_config to return a config with oidc=None.
        mock_config = SimpleNamespace(production=SimpleNamespace(oidc=None))
        monkeypatch.setattr("deerflow.config.app_config.get_app_config", lambda: mock_config)
        request = SimpleNamespace()
        result = await _resolve_oidc_bearer(request, "some.jwt.token")
        assert result is None

    @pytest.mark.anyio
    async def test_returns_401_on_invalid_token(self, monkeypatch):
        """When OIDC is configured but the token is invalid → 401 JSONResponse."""
        from app.gateway.auth.oidc import OidcTokenError
        from app.gateway.auth_middleware import _resolve_oidc_bearer

        oidc_config = SimpleNamespace(
            issuer="https://idp.example.com",
            audience="test",
            jwks_uri="https://idp.example.com/jwks",
            group_claim="groups",
            clock_skew_seconds=30,
            algorithms=["RS256"],
        )
        mock_config = SimpleNamespace(production=SimpleNamespace(oidc=oidc_config))
        monkeypatch.setattr("deerflow.config.app_config.get_app_config", lambda: mock_config)

        # Mock verify_oidc_token to return an error.
        import app.gateway.auth.oidc as oidc_module

        monkeypatch.setattr(
            oidc_module,
            "verify_oidc_token",
            lambda token, config: OidcTokenError.EXPIRED,
        )

        request = SimpleNamespace()
        result = await _resolve_oidc_bearer(request, "expired.jwt.token")
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401
