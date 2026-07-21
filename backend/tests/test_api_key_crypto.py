"""Tests for the API Key crypto module (PR-035).

Covers :mod:`app.gateway.auth.api_key`: plaintext format, HMAC hash
versioning, constant-time verify, pepper isolation, fail-closed on
malformed input. The pepper is loaded through ``AuthConfig`` so a test
fixture swaps it deterministically.

IAM IDs: ``IAM-330`` series (crypto layer; repository is ``IAM-32x``,
auth middleware is ``IAM-34x``).
"""

from __future__ import annotations

import pytest

from app.gateway.auth.api_key import (
    _DISPLAY_PREFIX,
    _RANDOM_PREFIX_LEN,
    generate_api_key,
    hash_api_key,
    needs_rehash,
    verify_api_key,
)


@pytest.fixture(autouse=True)
def _fixed_pepper(monkeypatch):
    """Pin the pepper to a known value so hash comparisons are deterministic.

    Bypasses the file-persisted pepper by pre-seeding the global config
    cache. Restoring the cache on teardown keeps other test modules that
    hit the real pepper (via integration paths) unaffected.
    """
    from app.gateway.auth import config as auth_config

    fixed = auth_config.AuthConfig(jwt_secret="jwt-test", api_key_pepper="test-pepper-fixed")
    auth_config.set_auth_config(fixed)
    yield
    auth_config.set_auth_config.__wrapped__ if hasattr(auth_config.set_auth_config, "__wrapped__") else None
    # Reset by clearing the module-level cache so the next caller
    # regenerates from env / file. The set_auth_config above replaced
    # the singleton; dropping it forces re-initialisation.
    auth_config._auth_config = None  # type: ignore[attr-defined]


class TestGenerateApiKey:
    def test_plaintext_format(self):
        plaintext, prefix, key_hash = generate_api_key()
        # dk_live_<prefix8>_<secret43>
        assert plaintext.startswith(_DISPLAY_PREFIX)
        parts = plaintext.split("_")
        # ["dk", "live", "<prefix8>", "<secret43>"]
        assert parts[0] == "dk"
        assert parts[1] == "live"
        assert len(parts[2]) == _RANDOM_PREFIX_LEN
        assert len(parts[3]) >= 40  # token_urlsafe(32) yields ~43 chars
        assert plaintext[len(_DISPLAY_PREFIX)] == parts[2][0]  # prefix contiguous

    def test_key_prefix_is_first_16_chars(self):
        plaintext, prefix, _ = generate_api_key()
        assert prefix == plaintext[: len(_DISPLAY_PREFIX) + _RANDOM_PREFIX_LEN]
        assert len(prefix) == 16

    def test_key_hash_versioned(self):
        _, _, key_hash = generate_api_key()
        assert key_hash.startswith("$dfakv1$")
        # hex digest is 64 chars after the prefix
        assert len(key_hash[len("$dfakv1$") :]) == 64

    def test_two_calls_yield_different_keys(self):
        a = generate_api_key()
        b = generate_api_key()
        assert a[0] != b[0]
        assert a[1] != b[1]
        assert a[2] != b[2]


class TestVerifyApiKey:
    def test_round_trip(self):
        plaintext, _, key_hash = generate_api_key()
        assert verify_api_key(plaintext, key_hash) is True

    def test_wrong_plaintext_rejects(self):
        plaintext, _, key_hash = generate_api_key()
        assert verify_api_key(plaintext + "x", key_hash) is False

    def test_truncated_plaintext_rejects(self):
        plaintext, _, key_hash = generate_api_key()
        assert verify_api_key(plaintext[:-5], key_hash) is False

    def test_tampered_hash_rejects(self):
        plaintext, _, key_hash = generate_api_key()
        tampered = key_hash[:-1] + ("0" if key_hash[-1] != "0" else "1")
        assert verify_api_key(plaintext, tampered) is False

    def test_malformed_hash_rejects(self):
        plaintext, _, _ = generate_api_key()
        assert verify_api_key(plaintext, "garbage") is False
        assert verify_api_key(plaintext, "") is False
        assert verify_api_key(plaintext, "$unknown_version$abc") is False

    def test_empty_inputs_rejects(self):
        assert verify_api_key("", "$dfakv1$abc") is False
        assert verify_api_key("dk_live_x_y", "") is False

    def test_pepper_isolation(self):
        """A hash computed with one pepper must NOT verify under another."""
        plaintext, _, key_hash = generate_api_key()
        # Swap pepper, recompute the hash of the SAME plaintext.
        from app.gateway.auth import config as auth_config

        original = auth_config.get_auth_config()
        try:
            auth_config.set_auth_config(auth_config.AuthConfig(jwt_secret="jwt-test", api_key_pepper="different-pepper"))
            assert verify_api_key(plaintext, key_hash) is False
            # The new pepper produces a different but valid hash.
            new_hash = hash_api_key(plaintext)
            assert new_hash != key_hash
            assert verify_api_key(plaintext, new_hash) is True
        finally:
            auth_config.set_auth_config(original)


class TestNeedsRehash:
    def test_v1_hash_needs_no_rehash(self):
        _, _, key_hash = generate_api_key()
        assert needs_rehash(key_hash) is False

    def test_unknown_version_needs_rehash(self):
        assert needs_rehash("$dfakv99$abc") is True
        assert needs_rehash("garbage") is True


class TestEntropySanity:
    """Defensive: prefix collision resistance within reasonable sample size.

    2^48 prefix space → birthday bound at ~2^24 = 16M keys. Sampling
    10k keys should never collide. This is a smoke test, not a proof.
    """

    def test_no_prefix_collision_in_10k_samples(self):
        prefixes = {generate_api_key()[1] for _ in range(10_000)}
        assert len(prefixes) == 10_000
