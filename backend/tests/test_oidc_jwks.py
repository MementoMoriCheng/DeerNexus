"""Tests for the OIDC JWKS bearer-JWT verification module (``auth/oidc.py``).

Uses a self-generated RSA keypair + a local mock JWKS HTTP server to test
the full verify_oidc_token flow: successful verification, expired tokens,
wrong issuer/audience, unknown kid, and custom group-claim names.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.gateway.auth.oidc import (
    OidcClaims,
    OidcTokenError,
    _reset_jwks_cache,
    verify_oidc_token,
)
from deerflow.config.production_config import ProductionOidcConfig

# ---------------------------------------------------------------------------
# Fixtures: self-generated RSA keypair + mock JWKS server
# ---------------------------------------------------------------------------


def _generate_rsa_keypair():
    """Generate an RSA keypair for RS256 signing/testing."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


def _int_to_base64url(n: int) -> str:
    """Convert a big integer to a base64url-encoded string (no padding)."""
    byte_len = (n.bit_length() + 7) // 8
    raw = n.to_bytes(byte_len, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _public_key_to_jwk(public_key, kid: str = "test-key-1") -> dict:
    """Convert a cryptography RSA public key to a JWK dict (RFC 7517)."""
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _int_to_base64url(numbers.n),
        "e": _int_to_base64url(numbers.e),
    }


class _MockJwksServer:
    """Minimal HTTP server serving a JWKS document at /jwks."""

    def __init__(self, jwks: dict):
        self._jwks = jwks
        self._server: HTTPServer | None = None
        self._thread: Thread | None = None
        self.url: str = ""

    def start(self):
        jwks_data = self._jwks

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/jwks":
                    body = json.dumps(jwks_data).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):  # suppress stderr noise
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/jwks"
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)


@pytest.fixture
def oidc_setup():
    """Set up RSA keypair + mock JWKS server + OIDC config."""
    _reset_jwks_cache()
    private_key, public_key = _generate_rsa_keypair()
    kid = "test-key-1"
    jwk = _public_key_to_jwk(public_key, kid=kid)
    jwks = {"keys": [jwk]}

    server = _MockJwksServer(jwks)
    server.start()

    issuer = "https://idp.example.com"
    audience = "deernexus-test"

    config = ProductionOidcConfig(
        issuer=issuer,
        audience=audience,
        jwks_uri=server.url,
    )

    yield {
        "private_key": private_key,
        "public_key": public_key,
        "kid": kid,
        "issuer": issuer,
        "audience": audience,
        "config": config,
    }

    _reset_jwks_cache()
    server.stop()


def _make_token(
    private_key,
    *,
    kid: str = "test-key-1",
    iss: str = "https://idp.example.com",
    aud: str = "deernexus-test",
    exp_delta: timedelta = timedelta(hours=1),
    groups: list[str] | None = None,
    group_claim: str = "groups",
    sub: str = "user-123",
    extra_claims: dict | None = None,
) -> str:
    """Sign a test RS256 JWT."""
    now = datetime.now(UTC)
    payload = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "iat": now,
        "exp": now + exp_delta,
    }
    payload[group_claim] = groups if groups is not None else ["engineers"]
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": kid},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVerifyOidcToken:
    def test_successful_verification(self, oidc_setup):
        """Valid RS256 token with correct iss/aud → OidcClaims."""
        token = _make_token(oidc_setup["private_key"])
        result = verify_oidc_token(token, oidc_setup["config"])
        assert isinstance(result, OidcClaims)
        assert result.sub == "user-123"
        assert result.iss == oidc_setup["issuer"]
        assert result.aud == oidc_setup["audience"]
        assert result.groups == ["engineers"]

    def test_expired_token(self, oidc_setup):
        """Token with past exp → EXPIRED (beyond clock-skew leeway)."""
        token = _make_token(oidc_setup["private_key"], exp_delta=timedelta(seconds=-100))
        result = verify_oidc_token(token, oidc_setup["config"])
        assert result is OidcTokenError.EXPIRED

    def test_wrong_issuer(self, oidc_setup):
        """Token from untrusted issuer → UNTRUSTED_ISSUER."""
        token = _make_token(oidc_setup["private_key"], iss="https://evil.example.com")
        result = verify_oidc_token(token, oidc_setup["config"])
        assert result is OidcTokenError.UNTRUSTED_ISSUER

    def test_wrong_audience(self, oidc_setup):
        """Token for wrong audience → AUDIENCE_MISMATCH."""
        token = _make_token(oidc_setup["private_key"], aud="some-other-app")
        result = verify_oidc_token(token, oidc_setup["config"])
        assert result is OidcTokenError.AUDIENCE_MISMATCH

    def test_unknown_kid(self, oidc_setup):
        """Token with a kid not in JWKS → KEY_NOT_FOUND."""
        token = _make_token(oidc_setup["private_key"], kid="unknown-kid")
        result = verify_oidc_token(token, oidc_setup["config"])
        assert result is OidcTokenError.KEY_NOT_FOUND

    def test_malformed_token(self, oidc_setup):
        """Dot-separated but invalid token → failure (KEY_NOT_FOUND or MALFORMED)."""
        result = verify_oidc_token("not.a.jwt", oidc_setup["config"])
        assert result in (OidcTokenError.KEY_NOT_FOUND, OidcTokenError.MALFORMED)

    def test_completely_garbage(self, oidc_setup):
        """Random non-JWT string → KEY_NOT_FOUND or MALFORMED."""
        result = verify_oidc_token("garbage", oidc_setup["config"])
        assert result in (OidcTokenError.KEY_NOT_FOUND, OidcTokenError.MALFORMED)

    def test_clock_skew_tolerance(self, oidc_setup):
        """Token slightly expired within leeway → success."""
        config = ProductionOidcConfig(
            issuer=oidc_setup["issuer"],
            audience=oidc_setup["audience"],
            jwks_uri=oidc_setup["config"].jwks_uri,
            clock_skew_seconds=60,
        )
        # Expired 30 seconds ago — within 60s leeway.
        token = _make_token(oidc_setup["private_key"], exp_delta=timedelta(seconds=-30))
        result = verify_oidc_token(token, config)
        assert isinstance(result, OidcClaims)

    def test_custom_group_claim(self, oidc_setup):
        """Group claim under a non-default field name is extracted."""
        config = ProductionOidcConfig(
            issuer=oidc_setup["issuer"],
            audience=oidc_setup["audience"],
            jwks_uri=oidc_setup["config"].jwks_uri,
            group_claim="memberOf",
        )
        token = _make_token(
            oidc_setup["private_key"],
            groups=["admins", "devs"],
            group_claim="memberOf",
        )
        result = verify_oidc_token(token, config)
        assert isinstance(result, OidcClaims)
        assert result.groups == ["admins", "devs"]

    def test_string_groups_claim(self, oidc_setup):
        """A string-valued groups claim is wrapped into a list."""
        token = _make_token(
            oidc_setup["private_key"],
            groups="single-group",
        )
        # _make_token always sets the claim as a list; override with string.
        now = datetime.now(UTC)
        payload = {
            "sub": "user-123",
            "iss": oidc_setup["issuer"],
            "aud": oidc_setup["audience"],
            "iat": now,
            "exp": now + timedelta(hours=1),
            "groups": "single-group",  # string, not list
        }
        token = jwt.encode(payload, oidc_setup["private_key"], algorithm="RS256", headers={"kid": oidc_setup["kid"]})
        result = verify_oidc_token(token, oidc_setup["config"])
        assert isinstance(result, OidcClaims)
        assert result.groups == ["single-group"]

    def test_no_jwks_uri_configured(self):
        """config with jwks_uri=None → MALFORMED (cannot verify)."""
        config = ProductionOidcConfig(
            issuer="https://idp.example.com",
            audience="test",
            jwks_uri=None,
        )
        result = verify_oidc_token("some.token.here", config)
        assert result is OidcTokenError.MALFORMED
