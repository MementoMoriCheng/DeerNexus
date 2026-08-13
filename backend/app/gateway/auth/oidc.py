"""OIDC bearer JWT (RS256) verification via JWKS.

Complements the existing HS256 session-cookie path (``jwt.py``) with an
asymmetric path for external Identity Provider (IdP) bearer tokens. The
session-cookie JWT is issued *by us* and signed with our symmetric secret;
the OIDC bearer JWT is issued *by the IdP* and signed with a private key
whose public counterpart is published in the IdP's JWKS endpoint.

Verification flow (``verify_oidc_token``):

1. ``jwt.PyJWKClient(config.jwks_uri)`` fetches and caches the JWKS
   document (300 s internal cache with automatic key rotation — PyJWT ≥
   2.5 provides this out of the box, no manual TTL management needed).
2. ``PyJWKClient.get_signing_key_from_jwt(token)`` extracts the ``kid``
   from the JWT header and locates the matching JWK (raises if the kid
   is unknown — key rotation, revoked IdP key, or tampered header).
3. ``jwt.decode(...)`` validates signature + ``iss`` + ``aud`` + ``exp``
   with configurable clock-skew leeway.
4. On success the validated claims are returned as :class:`OidcClaims`;
   every failure mode is mapped to :class:`OidcTokenError` so the caller
   (auth middleware) can render a 401 without distinguishing error types
   (anti-enumeration — same body for "bad issuer" as "bad signature").

This module is **synchronous** because ``PyJWKClient`` is synchronous
(it uses ``urllib`` under the hood for JWKS fetch). The auth middleware
wraps calls with ``asyncio.to_thread`` so the event loop is not blocked
during JWKS fetches.

The real OIDC code-flow / login-callback (security baseline §3.1) is
out of scope here — this module only verifies bearer tokens that clients
present directly. It pairs with the PR-036 group-mapping engine
(``oidc_group_mapping.apply_group_mapping``), which takes the verified
``(issuer, groups, subject)`` triple and turns it into ``role_bindings``.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

import jwt
from pydantic import BaseModel

if TYPE_CHECKING:
    from deerflow.config.production_config import ProductionOidcConfig

logger = logging.getLogger(__name__)


class OidcTokenError(StrEnum):
    """Exhaustive list of OIDC token verification failure reasons."""

    EXPIRED = "expired"
    INVALID_SIGNATURE = "invalid_signature"
    MALFORMED = "malformed"
    UNTRUSTED_ISSUER = "untrusted_issuer"
    AUDIENCE_MISMATCH = "audience_mismatch"
    KEY_NOT_FOUND = "key_not_found"


class OidcClaims(BaseModel):
    """Validated claims extracted from an OIDC bearer JWT."""

    sub: str
    iss: str
    aud: str
    groups: list[str]
    exp: int


# Module-level JWKS client cache keyed by ``jwks_uri``. ``PyJWKClient``
# internally caches keys for 300 s and refreshes on kid-miss, so a single
# client per IdP is both correct and efficient. The cache is lazy: the
# first token for a given IdP pays the JWKS fetch; subsequent tokens in
# the cache window reuse the cached keys.
_jwks_clients: dict[str, jwt.PyJWKClient] = {}


def _get_jwks_client(jwks_uri: str) -> jwt.PyJWKClient:
    """Return a cached ``PyJWKClient`` for ``jwks_uri``."""
    client = _jwks_clients.get(jwks_uri)
    if client is None:
        client = jwt.PyJWKClient(jwks_uri)
        _jwks_clients[jwks_uri] = client
    return client


def _reset_jwks_cache() -> None:
    """Clear the JWKS client cache (test-only)."""
    _jwks_clients.clear()


def verify_oidc_token(token: str, config: ProductionOidcConfig) -> OidcClaims | OidcTokenError:
    """Verify an OIDC bearer JWT against the configured IdP JWKS.

    Args:
        token: The raw JWT string (``header.payload.signature``).
        config: The ``ProductionOidcConfig`` declaring ``issuer``,
            ``audience``, ``jwks_uri``, and optional ``group_claim``,
            ``clock_skew_seconds``, ``algorithms``.

    Returns:
        :class:`OidcClaims` on success, or :class:`OidcTokenError` on
        any failure. Never raises — all exceptions are caught and mapped.
    """
    if config.jwks_uri is None:
        return OidcTokenError.MALFORMED

    try:
        signing_key = _get_jwks_client(config.jwks_uri).get_signing_key_from_jwt(token)
    except jwt.PyJWKClientError:
        return OidcTokenError.KEY_NOT_FOUND
    except Exception:  # noqa: BLE001 — network error fetching JWKS, malformed header, etc.
        logger.debug("JWKS key resolution failed", exc_info=True)
        return OidcTokenError.KEY_NOT_FOUND

    algorithms = config.algorithms if config.algorithms else ["RS256"]
    try:
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=algorithms,
            audience=config.audience,
            issuer=config.issuer,
            leeway=config.clock_skew_seconds,
        )
    except jwt.ExpiredSignatureError:
        return OidcTokenError.EXPIRED
    except jwt.InvalidAudienceError:
        return OidcTokenError.AUDIENCE_MISMATCH
    except jwt.InvalidIssuerError:
        return OidcTokenError.UNTRUSTED_ISSUER
    except (jwt.InvalidSignatureError, jwt.InvalidAlgorithmError):
        return OidcTokenError.INVALID_SIGNATURE
    except jwt.PyJWKError:
        return OidcTokenError.KEY_NOT_FOUND
    except jwt.PyJWTError:
        logger.debug("OIDC token decode failed", exc_info=True)
        return OidcTokenError.MALFORMED

    # Extract groups from the configured claim name (different IdPs use
    # different claim names: Keycloak → ``groups``, AD → ``groups``, Auth0
    # → sometimes ``permissions`` or ``roles`` via a custom claim).
    raw_groups = payload.get(config.group_claim, [])
    if isinstance(raw_groups, str):
        groups = [raw_groups]
    elif isinstance(raw_groups, list):
        groups = [str(g) for g in raw_groups]
    else:
        groups = []

    return OidcClaims(
        sub=str(payload.get("sub", "")),
        iss=str(payload.get("iss", "")),
        aud=str(payload.get("aud", "")),
        groups=groups,
        exp=int(payload.get("exp", 0)),
    )
