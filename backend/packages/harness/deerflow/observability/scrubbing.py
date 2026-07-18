"""Log-record scrubbing for forbidden fields (observability-and-slo §3.3).

The §3.3 forbidden-field list (Authorization / Cookie / API Key; Secret /
Token / DSN; full Prompt / Response; 文件正文; signed URL query; OIDC 完整
claims; Connector 原始敏感结果) is enforced here as a single choke-point used
by both the JSON formatter and ``emit_event``.

Matching rule (token-aware, case-insensitive): a key matches a forbidden word
when the word appears as a **full element** after splitting the key on
non-alphanumeric characters, OR the word is multi-word and appears as a
contiguous substring (so ``api_key`` matches ``api_key`` directly and
``bearer_token`` matches because it splits into ``["bearer", "token"]``
containing ``token``). This catches the three real-world key shapes —
``authorization`` (exact), ``bearer_token`` (suffixed single word), and
``httpx_authorization`` (prefixed) — without over-matching benign plural
fields like ``tokens`` (a count) or ``responses`` (a list of status objects).

``prompt`` and ``response`` are included because §3.3 forbids logging full
prompt / response bodies; token-aware matching lets ``response_status``
through (splits to ``["response", "status"]`` — ``response`` IS a full
element here, so it DOES match; the operator who needs a benign field must
name it ``status_code`` rather than ``response_status``). When a future
benign field collides, prefer renaming the call-site key rather than
weakening this gate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

# Lower-case forbidden words/phrases; matched case-insensitively. Each entry
# maps to a §3.3 bullet so audit of the list is audit of the spec.
FORBIDDEN_EXTRA_KEYS: Final[tuple[str, ...]] = (
    "authorization",
    "cookie",
    "api_key",
    "apikey",  # alternate spelling used by some SDKs (no underscore)
    "secret",
    "token",
    "dsn",
    "password",
    "passwd",
    "prompt",
    "response",
    "claims",  # OIDC 完整 claims
    "file_body",  # 文件正文
    "signed_url",  # 签名 URL query
)

# Value emitted for a forbidden key. We do NOT echo the original value (even
# truncated) — truncation can still leak the first bytes of a secret.
_REDACTED: Final[str] = "<redacted>"

# Pre-compute single-word vs multi-word forbidden entries. Single words use
# the set-membership fast path; multi-word entries fall back to substring.
_FORBIDDEN_SINGLE_WORDS: Final[frozenset[str]] = frozenset(word for word in FORBIDDEN_EXTRA_KEYS if "_" not in word)
_FORBIDDEN_MULTI_WORD: Final[tuple[str, ...]] = tuple(word for word in FORBIDDEN_EXTRA_KEYS if "_" in word)

# Split a key into its alphanumeric tokens. ``bearer_token`` → {"bearer","token"},
# ``response-status`` → {"response","status"}, ``tokens`` → {"tokens"}.
_TOKEN_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


def looks_forbidden(key: str) -> bool:
    """Return ``True`` if *key* matches a forbidden §3.3 word (case-insensitive).

    Token-aware: a single-word forbidden entry (``token``, ``secret``, …)
    matches when it appears as a full token element of the key after splitting
    on non-alphanumerics; this catches ``bearer_token`` / ``httpx_authorization``
    / ``sqlalchemy_password`` but not the benign plural ``tokens``. Multi-word
    entries (``api_key``, ``file_body``, ``signed_url``) match as contiguous
    substrings since the underscore is itself the natural separator.
    """
    if not isinstance(key, str):  # non-string keys cannot enable substring attacks
        return False
    lowered = key.lower()
    # Fast path: multi-word forbidden phrases via substring.
    for phrase in _FORBIDDEN_MULTI_WORD:
        if phrase in lowered:
            return True
    # Single-word forbidden entries via token-membership.
    tokens = set(_TOKEN_SPLIT_RE.split(lowered))
    return bool(tokens & _FORBIDDEN_SINGLE_WORDS)


def scrub_extra(extra: Mapping[str, object] | None) -> dict[str, object]:
    """Return a copy of *extra* with forbidden keys redacted.

    Forbidden keys are kept (with ``"<redacted>"`` value) rather than dropped
    so the reader of a log line can see that the call site attempted to log
    something there and the scrubber intervened — silent drop would let a
    future regression pass unobserved. Non-forbidden keys pass through
    unchanged.

    ``None`` (the common case of plain ``logger.info("msg")`` calls with no
    ``extra=``) returns an empty dict so callers always get a mapping.
    """
    if not extra:
        return {}
    return {str(key): _REDACTED if looks_forbidden(str(key)) else value for key, value in extra.items()}


__all__ = ["FORBIDDEN_EXTRA_KEYS", "looks_forbidden", "scrub_extra"]
