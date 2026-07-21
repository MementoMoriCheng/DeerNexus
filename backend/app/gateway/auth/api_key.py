"""API Key generation and verification utilities (PR-035).

Hash format: ``$dfakv<N>$<hex_digest>`` where ``<N>`` is the version.
Mirrors ``app.gateway.auth.password``'s versioned-hash convention so a
future algorithm change can ship alongside v1 keys without breaking
verification.

- **v1** (current): ``HMAC-SHA256(pepper, plaintext).hex()`` — keyed
  hash with a server-side pepper. The pepper prevents a DB-leak-only
  offline brute force (an attacker who exfiltrates ``api_keys.key_hash``
  still cannot forge a key without the pepper). HMAC is constant-time-
  friendly via ``hmac.compare_digest``.

Why HMAC instead of bcrypt (the password pattern)?

* API keys are high-entropy (256 bits in the secret portion) — the
  rate-limiting protection bcrypt provides for low-entropy human
  passwords is unnecessary.
* Auth runs on every API-key-authenticated request; bcrypt's ~100 ms
  cost would dominate request latency. HMAC-SHA256 is microseconds.
* ADR §9.2 explicitly allows "强单向哈希 / HMAC".

Plaintext format: ``dk_live_<prefix8>_<secret43>`` (60 chars total).

* ``dk_live_`` is a fixed 8-char displayable prefix (``dk`` = DeerNexus;
  ``live`` identifies the environment; swap for ``dk_test_`` in
  non-prod when needed). Stored in plaintext in the DB.
* ``<prefix8>`` is 8 chars of ``secrets.token_urlsafe`` entropy and
  doubles as the ``key_prefix`` lookup key (``uq_api_keys_key_prefix``
  unique index). The DB stores ``dk_live_<prefix8>`` (16 chars) so
  operators can eyeball the environment from a prefix dump.
* ``<secret43>`` is ``secrets.token_urlsafe(32)`` = 43 chars (~256
  bits). NEVER stored; only its HMAC lands in the DB.

The plaintext is returned exactly once (ADR §9.2 "明文只展示一次") by
the mint endpoint and never appears in logs, traces, audit payloads,
or the frontend (ADR §9.2 line 302). ``contracts.events._FORBIDDEN_PAYLOAD_KEYS``
includes ``"api_key"`` / ``"key_hash"`` as defense-in-depth.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_CURRENT_VERSION = 1
_PREFIX_V1 = "$dfakv1$"

# Displayable prefix that begins every plaintext key. 8 chars so the
# 16-char DB ``key_prefix`` column holds ``_DISPLAY_PREFIX + <random8>``
# exactly (no truncation). The prefix is human-readable so an operator
# triaging a key prefix from logs/DB can tell at a glance which
# environment + product it belongs to.
_DISPLAY_PREFIX = "dk_live_"
_RANDOM_PREFIX_LEN = 8  # urlsafe chars; ~48 bits of prefix entropy
_SECRET_BYTES = 32  # → 43 urlsafe chars; 256 bits of secret entropy


def _get_pepper() -> bytes:
    """Return the AUTH_API_KEY_PEPPER as bytes.

    Loaded lazily through ``app.gateway.auth.config.get_auth_config``
    so test fixtures that swap the config via ``set_auth_config`` are
    picked up. The pepper is generated + persisted on first use if
    ``AUTH_API_KEY_PEPPER`` is unset, mirroring the ``AUTH_JWT_SECRET``
    pattern. It MUST NOT be reused as the JWT signing secret
    (defense-in-depth: a JWT implementation bug must not leak the HMAC
    pepper).
    """
    from app.gateway.auth.config import get_auth_config

    return get_auth_config().api_key_pepper.encode("utf-8")


def _hmac_sha256_hex(plaintext: str) -> str:
    """Compute ``HMAC-SHA256(pepper, plaintext)`` as lowercase hex."""
    return hmac.new(_get_pepper(), plaintext.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Mint a new API key. Returns ``(plaintext, key_prefix, key_hash)``.

    The plaintext is returned to the caller exactly once and NEVER
    persisted (only ``key_hash`` lands in the DB). ``key_prefix`` is the
    first 16 chars of the plaintext (``dk_live_<random8>``) and is the
    lookup key for the ``uq_api_keys_key_prefix`` unique index.

    Callers MUST treat the returned ``plaintext`` as a secret: surface
    it in exactly one HTTP response, never log it, never persist it.
    """
    # 8 urlsafe chars → ~48 bits; collision probability within one Org's
    # key set is negligible (birthday bound at 2^24 keys).
    random_prefix = secrets.token_urlsafe(_RANDOM_PREFIX_LEN)[:_RANDOM_PREFIX_LEN]
    # 43 urlsafe chars; 256 bits of entropy in the secret portion.
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    plaintext = f"{_DISPLAY_PREFIX}{random_prefix}_{secret}"
    key_prefix = plaintext[: len(_DISPLAY_PREFIX) + _RANDOM_PREFIX_LEN]
    key_hash = hash_api_key(plaintext)
    return plaintext, key_prefix, key_hash


def hash_api_key(plaintext: str) -> str:
    """Hash a plaintext key for storage. Only the mint + tests call this.

    Versioned so a future algorithm change can ship alongside v1 keys.
    """
    return f"{_PREFIX_V1}{_hmac_sha256_hex(plaintext)}"


def verify_api_key(plaintext: str, stored_hash: str) -> bool:
    """Constant-time verify a plaintext key against its stored hash.

    Returns ``False`` (never raises) on a malformed ``stored_hash`` or a
    verification failure — fail closed at the call site by treating
    ``False`` as ``401 authentication_invalid`` (ADR §12 first paragraph).

    Constant-time: ``hmac.compare_digest`` over the recomputed HMAC hex
    digest. The recompute + compare pattern is preferred over comparing
    the full ``stored_hash`` strings because the version prefix leaks
    length / version info that could otherwise short-circuit.
    """
    if not plaintext or not stored_hash:
        return False
    if not stored_hash.startswith(_PREFIX_V1):
        # Unknown version — fail closed rather than guess.
        return False
    try:
        recomputed = _hmac_sha256_hex(plaintext)
    except Exception:  # noqa: BLE001 — pepper load failure, encoding error, etc.
        return False
    stored_digest = stored_hash[len(_PREFIX_V1) :]
    return hmac.compare_digest(recomputed, stored_digest)


def needs_rehash(stored_hash: str) -> bool:
    """Return True if the hash uses an older version and should be rehashed.

    Mirrors ``password.needs_rehash`` for symmetry. Today only v1 exists
    so this always returns False, but the surface lets a future v2 land
    without touching call sites.
    """
    return not stored_hash.startswith(_PREFIX_V1)


__all__ = [
    "generate_api_key",
    "hash_api_key",
    "needs_rehash",
    "verify_api_key",
]
